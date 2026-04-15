"""
Webhook RECEIVER — the service that listens for incoming webhooks.

Run with: uvicorn receiver:app --port 9000 --reload

Key concepts demonstrated:
  - Receiving POST requests with JSON payloads
  - Verifying HMAC-SHA256 signatures (reject tampered/fake requests)
  - Idempotency (detecting duplicate deliveries)
  - Responding quickly with 200 (acknowledge first, process later in production)

Think of this as YOUR service — you registered a URL with GitHub/Stripe/etc.,
and now their webhooks are hitting your endpoint.
"""

# hashlib provides the SHA-256 algorithm we use for signature verification
import hashlib
# hmac lets us compute and compare HMAC signatures securely
import hmac
# datetime for timestamping when we received each event
from datetime import datetime, timezone

# FastAPI is our web framework
from fastapi import FastAPI, Header, HTTPException, Request
# Header: extracts values from HTTP headers (like X-Webhook-Signature)
# HTTPException: lets us return error responses (like 401 Unauthorized)
# Request: gives us access to the raw request body

# Create the FastAPI application instance for the receiver
app = FastAPI(title="Webhook Receiver")

# ── Shared secret ────────────────────────────────────────────────────────────
# This MUST match the sender's secret. In production, you'd receive this
# during the subscription process (the sender gives you a secret when you
# register your webhook URL).
WEBHOOK_SECRET = "super-secret-key"

# ── In-memory stores ────────────────────────────────────────────────────────
# received_events: a list of all webhook events we've successfully processed
received_events: list[dict] = []
# processed_ids: a set of webhook IDs we've already handled
# We use a set because lookups are O(1) — fast even with millions of entries
# This is how we implement idempotency (don't process the same event twice)
processed_ids: set[str] = set()


def verify_signature(payload: bytes, signature_header: str, secret: str) -> bool:
    """
    Verify the HMAC-SHA256 signature sent by the webhook sender.

    This is CRITICAL in production — without this check, anyone who discovers
    your webhook URL could POST fake events and trick your system into
    processing fraudulent data (e.g., fake payment confirmations).

    How it works:
    1. The sender computed HMAC-SHA256(secret, payload) and sent it in a header
    2. We compute the same thing with our copy of the secret
    3. If they match, the payload is authentic and untampered

    Args:
        payload: The raw request body bytes (exactly as received)
        signature_header: The X-Webhook-Signature header value (e.g., "sha256=abc123...")
        secret: Our copy of the shared secret
    Returns:
        True if the signature is valid, False otherwise
    """
    # The signature header should start with "sha256=" — reject if it doesn't
    if not signature_header.startswith("sha256="):
        return False

    # Extract just the hex signature part (everything after "sha256=")
    received_sig = signature_header[len("sha256="):]

    # Compute what the signature SHOULD be using our secret and the payload
    expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    # Compare the two signatures using hmac.compare_digest()
    # Why not just use == ? Because == can leak timing information.
    # An attacker could measure how long the comparison takes to figure out
    # how many characters of the signature they got right. compare_digest()
    # always takes the same amount of time regardless of where the mismatch is.
    return hmac.compare_digest(received_sig, expected_sig)


@app.post("/webhook")
async def receive_webhook(
    request: Request,
    # These Header() parameters tell FastAPI to extract values from HTTP headers.
    # The ... means "required" — FastAPI will return 422 if the header is missing.
    # None means "optional" — it's okay if this header isn't present.
    x_webhook_signature: str = Header(...),   # Required: the HMAC signature
    x_webhook_id: str = Header(...),          # Required: unique ID for idempotency
    x_webhook_timestamp: str = Header(None),  # Optional: when the event was created
):
    """
    The main webhook endpoint. This is the URL you register with the sender.

    When the sender fires an event, it POSTs to this URL with:
    - The event data as JSON in the body
    - Signature, ID, and timestamp in the headers
    """
    # ── Step 1: Read the raw request body ────────────────────────────────
    # We need the raw bytes (not parsed JSON) because signature verification
    # must be done on the exact bytes that were signed. Parsing to JSON and
    # back could change whitespace/ordering and break the signature.
    body = await request.body()

    # ── Step 2: Verify the signature ─────────────────────────────────────
    # This is the most important security step. If the signature doesn't
    # match, either:
    #   a) The payload was tampered with in transit
    #   b) The request didn't come from the real sender (someone is faking it)
    #   c) The secrets don't match (misconfiguration)
    # In all cases, we reject the request with 401 Unauthorized.
    if not verify_signature(body, x_webhook_signature, WEBHOOK_SECRET):
        print("❌ Signature verification FAILED — rejecting request")
        raise HTTPException(status_code=401, detail="Invalid signature")

    print("✅ Signature verified")

    # ── Step 3: Parse the JSON payload ───────────────────────────────────
    # Now that we've verified the signature, it's safe to parse the body.
    # request.json() parses the body bytes as JSON into a Python dict.
    event = await request.json()

    # ── Step 4: Idempotency check ────────────────────────────────────────
    # Webhooks can be delivered MORE THAN ONCE. Why?
    #   - Network glitch: sender didn't get our 200 response, so it retries
    #   - Sender bug: accidentally fires the same event twice
    #   - Our server was slow: sender timed out and retried
    #
    # Without idempotency, you might process an order twice, send two emails,
    # charge a customer twice, etc. The webhook ID lets us detect duplicates.
    if x_webhook_id in processed_ids:
        print(f"⚠️  Duplicate delivery detected: {x_webhook_id} — skipping")
        # Return 200 (not an error!) — we already handled this successfully
        return {"status": "already_processed", "webhook_id": x_webhook_id}

    # Mark this webhook ID as processed so future duplicates are caught
    processed_ids.add(x_webhook_id)

    # ── Step 5: Process the event ────────────────────────────────────────
    # This is where your business logic goes. For example:
    #   - "order.created" → save the order to your database
    #   - "payment.completed" → mark the invoice as paid
    #   - "user.deleted" → clean up their data in your system
    #
    # In production, you'd typically push this to a message queue (like SQS
    # or RabbitMQ) and return 200 immediately. A background worker would
    # then process the event. This keeps response times fast.
    log_entry = {
        "webhook_id": x_webhook_id,                        # The unique webhook ID
        "event_type": event.get("event_type"),             # What happened
        "data": event.get("data"),                         # The event details
        "received_at": datetime.now(timezone.utc).isoformat(),  # When we got it
        "signature_valid": True,                           # We verified it above
    }
    # Store the event in our list (in production, this would be a database)
    received_events.append(log_entry)

    # Log to the console so you can see events arriving in real time
    print(f"📨 Received event: {event.get('event_type')} — {event.get('data')}")

    # ── Step 6: Respond with 200 quickly ─────────────────────────────────
    # The sender is waiting for our response! If we take too long (typically
    # 5-30 seconds depending on the sender), they'll assume we're down and
    # retry — which creates duplicate deliveries.
    #
    # Rule of thumb: acknowledge fast, process later.
    return {"status": "received", "webhook_id": x_webhook_id}


@app.get("/events")
async def list_received_events():
    """
    View all received webhook events.
    This is a convenience endpoint for learning and debugging — you can
    call this to see what webhooks have arrived.
    """
    return received_events
