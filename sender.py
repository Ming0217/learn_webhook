"""
Webhook SENDER — the EVENT PRODUCER in an event-driven architecture.

Run with: uvicorn sender:app --port 8000 --reload

=== What is event-driven architecture? ===

In traditional request-driven design, the consumer keeps asking:
  "Did anything happen yet?" over and over (this is called POLLING).

In event-driven design, the producer says:
  "Something just happened, here you go!" (this is called PUSHING).

The flow is inverted — the consumer doesn't ask, the producer announces.

Every event-driven system has three pieces:
  1. Event Producer  — detects that something happened and announces it
  2. Event Channel   — the mechanism that carries the event to consumers
  3. Event Consumer  — receives the event and reacts to it

=== How this file fits in ===

This file is the EVENT PRODUCER. It:
  - Lets consumers register their URLs (the subscription model)
  - Detects when something happens (the /events endpoint is called)
  - Pushes the event to all registered consumers over HTTP

The key event-driven design decisions here:
  - The receiver NEVER asks for data — it just waits
  - The sender DECIDES when to push — triggered by fire_event()
  - Loose coupling — the sender doesn't know what the receiver does with the event
  - Subscription model — consumers register interest in specific event types,
    and the sender filters accordingly

=== Webhooks vs. other event-driven patterns ===

Webhooks (what we built here):
  Producer POSTs directly to consumer over HTTP. Simple, no extra infrastructure.
  Downside: if the consumer is down, events can be lost (mitigated by retries).

Message queues (SQS, RabbitMQ):
  Producer puts events on a queue, consumer pulls at its own pace.
  The queue buffers events, so nothing is lost if the consumer is down.
  More reliable, but requires queue infrastructure.

Pub/sub (Kafka, SNS, Redis Pub/Sub):
  Producer publishes to a topic, multiple consumers subscribe.
  Like webhooks but with a broker handling fan-out, persistence, and replay.
  Most scalable, but most complex.

Server-Sent Events / WebSockets:
  Persistent connections where the server pushes to the client in real time.
  Used for live UIs (chat, dashboards), not service-to-service communication.

The progression is roughly: webhooks → message queues → pub/sub,
each adding reliability and complexity. Most real systems use a combination.
For example, Stripe sends you a webhook, but internally uses Kafka to
process millions of payment events.

=== Key concepts demonstrated ===

  - Managing subscriber URLs (subscription registry)
  - Signing payloads with HMAC-SHA256 so receivers can verify authenticity
  - Delivering webhooks with retry logic and exponential backoff
  - Async HTTP delivery using httpx

Think of this as a service like GitHub or Stripe — when something happens
(a push, a payment), it notifies everyone who registered a URL.
"""

# hashlib provides hashing algorithms (SHA-256, MD5, etc.)
import hashlib
# hmac implements keyed-hashing for message authentication (HMAC)
# This is how we "sign" payloads so the receiver can verify they came from us
import hmac
# json lets us serialize Python dicts into JSON strings (and back)
import json
# time gives us sleep() for pausing between retries
import time
# uuid generates unique identifiers — we use these as webhook IDs
import uuid
# datetime lets us create timestamps for when events are fired
from datetime import datetime, timezone

# httpx is an HTTP client library (like requests, but with async support)
# We use it to POST webhook payloads to subscriber URLs
import httpx
# FastAPI is the web framework — it handles routing HTTP requests to our functions
from fastapi import FastAPI, HTTPException
# Pydantic models validate incoming request data automatically
# BaseModel is the base class, HttpUrl ensures a field is a valid URL
from pydantic import BaseModel, HttpUrl

# Create the FastAPI application instance
# This is the main object that holds all our routes/endpoints
app = FastAPI(title="Webhook Sender")

# ── Shared secret (in production, each subscriber would have its own) ────────
# This secret is shared between sender and receiver.
# The sender uses it to sign payloads, the receiver uses it to verify.
# If someone intercepts the webhook, they can't forge a valid signature
# without knowing this secret.
WEBHOOK_SECRET = "super-secret-key"

# ── In-memory stores (use a real database in production) ─────────────────────
# subscribers: maps subscriber IDs to their config (URL + event types)
# Example: {"abc123": {"url": "http://localhost:9000/webhook", "events": ["order.created"]}}
subscribers: dict[str, dict] = {}
# delivery_log: records every delivery attempt (success or failure) for debugging
delivery_log: list[dict] = []


# ── Pydantic Models ──────────────────────────────────────────────────────────
# These define the shape of data we expect in incoming requests.
# FastAPI uses them to automatically validate request bodies and return
# clear error messages if the data doesn't match.

class SubscribeRequest(BaseModel):
    """What a client sends when they want to register for webhooks."""
    url: HttpUrl          # The URL we'll POST webhooks to — must be a valid URL
    events: list[str] = ["*"]  # Which event types they care about; "*" means all


class EventRequest(BaseModel):
    """What triggers a webhook — describes something that happened."""
    event_type: str  # A label like "order.created" or "user.signed_up"
    payload: dict    # The actual event data (order details, user info, etc.)


# ── Helper Functions ─────────────────────────────────────────────────────────

def sign_payload(payload_bytes: bytes, secret: str) -> str:
    """
    Create an HMAC-SHA256 signature for the payload.

    How it works:
    1. Take the raw payload bytes and the secret key
    2. Run them through HMAC-SHA256 (a keyed hash function)
    3. Return the hex string of the result

    The receiver will do the exact same computation with the same secret.
    If the signatures match, the payload hasn't been tampered with.
    """
    # hmac.new(key, message, hash_algorithm) creates the HMAC object
    # .hexdigest() converts the binary hash to a readable hex string
    return hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()


async def deliver_webhook(url: str, body: dict, max_retries: int = 3):
    """
    Deliver a webhook to a subscriber URL with retry + exponential backoff.

    Why retries? The receiver might be temporarily down, slow, or returning
    errors. We don't want to lose events, so we try multiple times with
    increasing delays between attempts (exponential backoff).

    Returns a log entry dict describing what happened.
    """
    # Convert the body dict to JSON bytes — this is what we'll send AND sign
    payload_bytes = json.dumps(body, default=str).encode()

    # Sign the payload so the receiver can verify it's authentic
    signature = sign_payload(payload_bytes, WEBHOOK_SECRET)

    # Build the HTTP headers that accompany the webhook
    headers = {
        "Content-Type": "application/json",          # Tell the receiver it's JSON
        "X-Webhook-Signature": f"sha256={signature}",  # The HMAC signature for verification
        "X-Webhook-Id": body["id"],                   # Unique ID so receiver can detect duplicates
        "X-Webhook-Timestamp": body["timestamp"],     # When the event was created (replay attack prevention)
    }

    # Track which attempt we're on and the last error we saw.
    # last_log_entry tracks whether the last attempt was logged inside the loop
    # (HTTP response received) or not (network exception — nothing logged yet).
    attempt = 0
    last_error = None
    last_log_entry = None  # Set when an HTTP response is received (even non-2xx)

    # Create an async HTTP client with a 10-second timeout per request
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Keep trying until we hit max_retries
        while attempt < max_retries:
            try:
                # Send the POST request to the subscriber's URL
                # content= sends raw bytes (not json=), because we need to send
                # the exact bytes we signed
                resp = await client.post(str(url), content=payload_bytes, headers=headers)

                # Build a log entry for this attempt
                log_entry = {
                    "webhook_id": body["id"],                        # Which webhook this was
                    "url": str(url),                                 # Where we sent it
                    "status_code": resp.status_code,                 # HTTP status we got back
                    "attempt": attempt + 1,                          # Which attempt number (1-based)
                    "success": 200 <= resp.status_code < 300,        # 2xx = success
                    "timestamp": datetime.now(timezone.utc).isoformat(),  # When this attempt happened
                }
                # Add to our delivery log for debugging
                delivery_log.append(log_entry)
                last_log_entry = log_entry  # Remember we already logged this attempt

                # If we got a 2xx response, delivery succeeded — we're done
                if log_entry["success"]:
                    return log_entry

                # Non-2xx response (like 500 Internal Server Error) — we'll retry
                last_error = f"HTTP {resp.status_code}"

            except httpx.RequestError as exc:
                # Network error (connection refused, timeout, DNS failure, etc.)
                # The receiver might be down — we'll retry.
                # Nothing is logged here; last_log_entry stays None for this attempt.
                last_error = str(exc)
                last_log_entry = None  # This attempt has no log entry yet

            # Move to the next attempt
            attempt += 1

            # If we have retries left, wait before trying again
            if attempt < max_retries:
                # Exponential backoff: wait 2s, then 4s, then 8s, etc.
                # This avoids hammering a struggling server
                backoff = 2 ** attempt
                print(f"⏳ Retry {attempt + 1} for {url} in {backoff}s ({last_error})")
                # Note: time.sleep() blocks the thread. In production you'd use
                # asyncio.sleep() instead. This is fine for learning.
                time.sleep(backoff)

    # If we get here, all retries are exhausted — delivery failed.
    # Only log a final failure entry if the last attempt was a network exception
    # (no HTTP response was received, so nothing was logged inside the loop).
    # If the last attempt got an HTTP error (like 401), it was already logged above.
    if last_log_entry is None:
        fail_entry = {
            "webhook_id": body["id"],
            "url": str(url),
            "status_code": None,                                # No HTTP response received
            "attempt": attempt,                                  # How many times we tried
            "success": False,                                    # Mark as failed
            "error": last_error,                                 # What went wrong
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        delivery_log.append(fail_entry)
        return fail_entry

    return last_log_entry
    return fail_entry


# ── API Endpoints ────────────────────────────────────────────────────────────
# These are the HTTP routes that clients can call.
# FastAPI decorators (@app.post, @app.get, etc.) map URLs to functions.

@app.post("/subscribers")
async def subscribe(req: SubscribeRequest):
    """
    Register a new webhook subscriber.

    A client calls this to say: "Hey, send webhooks to this URL when these
    events happen." This is like going to GitHub → Settings → Webhooks → Add.
    """
    # Generate a short unique ID for this subscriber
    sub_id = str(uuid.uuid4())[:8]
    # Store the subscriber's URL and which events they want
    subscribers[sub_id] = {"url": str(req.url), "events": req.events}
    # Return the subscriber info so the caller knows their ID
    return {"subscriber_id": sub_id, "url": str(req.url), "events": req.events}


@app.get("/subscribers")
async def list_subscribers():
    """List all registered subscribers. Useful for debugging."""
    return subscribers


@app.delete("/subscribers/{sub_id}")
async def unsubscribe(sub_id: str):
    """
    Remove a subscriber. They'll stop receiving webhooks.
    The {sub_id} in the URL is a path parameter — FastAPI extracts it automatically.
    """
    # Check if the subscriber exists
    if sub_id not in subscribers:
        # Return a 404 error if not found
        raise HTTPException(status_code=404, detail="Subscriber not found")
    # Remove them from our store
    del subscribers[sub_id]
    return {"deleted": sub_id}


@app.post("/events")
async def fire_event(req: EventRequest):
    """
    Fire an event — this is the trigger that causes webhooks to be sent.

    In a real app, this wouldn't be an API endpoint. Instead, your internal
    code would call this logic when something happens (e.g., after saving
    an order to the database). We expose it as an endpoint here so you
    can trigger it easily for learning.
    """
    # Build the webhook payload — this is what gets sent to every subscriber
    webhook_body = {
        "id": str(uuid.uuid4()),                          # Unique ID for this webhook delivery
        "event_type": req.event_type,                      # What happened (e.g., "order.created")
        "timestamp": datetime.now(timezone.utc).isoformat(),  # When it happened
        "data": req.payload,                               # The actual event data
    }

    # Deliver to every subscriber who cares about this event type
    results = []
    for sub_id, sub in subscribers.items():
        # Check if this subscriber wants this event type
        # "*" means they want everything, otherwise check for an exact match
        if "*" in sub["events"] or req.event_type in sub["events"]:
            # Deliver the webhook and collect the result
            result = await deliver_webhook(sub["url"], webhook_body)
            # Tag the result with the subscriber ID for the response
            results.append({"subscriber_id": sub_id, **result})

    # Return a summary of what happened
    return {
        "event_id": webhook_body["id"],       # The unique event ID
        "event_type": req.event_type,          # What type of event was fired
        "deliveries": results,                 # Delivery results for each subscriber
    }


@app.get("/deliveries")
async def get_delivery_log():
    """
    View the full delivery log.
    Shows every delivery attempt (successes, failures, retries) — great for debugging.
    """
    return delivery_log
