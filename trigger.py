"""
Trigger script — drives the full webhook flow end to end.

Run this AFTER starting both servers:
  Terminal 1: uvicorn sender:app --port 8000 --reload
  Terminal 2: uvicorn receiver:app --port 9000 --reload
  Terminal 3: python trigger.py

This script simulates what happens in the real world:
  1. Your service registers a webhook URL with a provider (like GitHub)
  2. Something happens on the provider (a push, a payment, etc.)
  3. The provider sends a webhook to your URL
  4. Your service receives and processes it

We play both roles here so you can see the entire flow.
"""

# httpx is an HTTP client library — we use it to call our sender and receiver APIs
import httpx
# time gives us sleep() to add a small delay between steps
import time

# Base URLs for our two servers
SENDER = "http://localhost:8000"    # The webhook sender runs on port 8000
RECEIVER = "http://localhost:9000"  # The webhook receiver runs on port 9000


def main():
    # Create an HTTP client with a 10-second timeout for all requests
    # This is like opening a browser — we'll use it to make HTTP calls
    client = httpx.Client(timeout=10.0)

    # ── Step 1: Register the receiver as a webhook subscriber ────────────
    # This is like going to GitHub → Settings → Webhooks → Add webhook
    # and entering your server's URL.
    print("\n📝 Step 1: Registering receiver as a subscriber...")
    resp = client.post(
        f"{SENDER}/subscribers",  # Call the sender's subscribe endpoint
        json={
            # The URL where the sender should POST webhooks to
            "url": f"{RECEIVER}/webhook",
            # Only send us these event types (ignore everything else)
            "events": ["order.created", "order.shipped"],
        },
    )
    # Print the response — should show our subscriber ID and config
    print(f"   Response: {resp.json()}")

    # ── Step 2: Fire an "order.created" event ────────────────────────────
    # In the real world, this would happen automatically when a customer
    # places an order. Here we trigger it manually via the API.
    print("\n🔥 Step 2: Firing 'order.created' event...")
    resp = client.post(
        f"{SENDER}/events",  # Call the sender's fire-event endpoint
        json={
            "event_type": "order.created",  # The type of event
            "payload": {                     # The event data — whatever is relevant
                "order_id": "ORD-12345",
                "customer": "Alice",
                "total": 99.99,
                "items": ["Widget A", "Gadget B"],
            },
        },
    )
    # The response tells us the event ID and delivery results
    result = resp.json()
    print(f"   Event ID: {result['event_id']}")
    # deliveries shows whether each subscriber received it successfully
    print(f"   Deliveries: {result['deliveries']}")

    # ── Step 3: Fire an "order.shipped" event ────────────────────────────
    # A second event for the same order — now it's been shipped.
    # The receiver subscribed to this event type too, so it should get it.
    print("\n🔥 Step 3: Firing 'order.shipped' event...")
    resp = client.post(
        f"{SENDER}/events",  # Same endpoint, different event type
        json={
            "event_type": "order.shipped",
            "payload": {
                "order_id": "ORD-12345",                  # Same order as before
                "tracking_number": "1Z999AA10123456784",  # New info: tracking number
            },
        },
    )
    result = resp.json()
    print(f"   Event ID: {result['event_id']}")
    print(f"   Deliveries: {result['deliveries']}")

    # ── Step 4: Check what the receiver got ──────────────────────────────
    # Now let's verify the receiver actually received and processed both events.
    # We call the receiver's /events endpoint to see its internal log.
    time.sleep(1)  # Small delay to let any async processing finish
    print("\n📬 Step 4: Checking receiver's received events...")
    resp = client.get(f"{RECEIVER}/events")  # GET the receiver's event log
    events = resp.json()
    # Loop through each received event and print it
    for evt in events:
        print(f"   ✅ {evt['event_type']}: {evt['data']}")

    # ── Step 5: Check sender's delivery log ──────────────────────────────
    # The sender keeps a log of every delivery attempt. This is useful for
    # debugging — you can see which deliveries succeeded, failed, or were retried.
    print("\n📋 Step 5: Sender's delivery log...")
    resp = client.get(f"{SENDER}/deliveries")  # GET the sender's delivery log
    for entry in resp.json():
        # Show a checkmark for success, X for failure
        status = "✅" if entry["success"] else "❌"
        # Print a summary of each delivery attempt
        print(
            f"   {status} {entry['webhook_id'][:8]}... → {entry['url']} "
            f"(attempt {entry['attempt']}, HTTP {entry.get('status_code', 'N/A')})"
        )

    # ── Done! ────────────────────────────────────────────────────────────
    print("\n🎉 Done! You just saw the full webhook flow end to end.")
    print("   Sender fired events → Signed the payloads → POSTed to receiver")
    print("   Receiver verified signatures → Checked for duplicates → Processed events\n")


# This is the standard Python entry point.
# It means: "only run main() if this file is executed directly (not imported)."
if __name__ == "__main__":
    main()
