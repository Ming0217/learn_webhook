# Learning Webhooks

A hands-on project for learning webhook concepts end to end. It includes a **sender** (the service that fires webhooks), a **receiver** (the service that listens for them), and a **trigger script** that drives the full flow.

Every line of code is commented so you can read each file like a tutorial.

## What You'll Learn

- How webhooks work (push model vs. polling)
- Registering subscriber URLs
- Signing payloads with HMAC-SHA256 for authenticity
- Verifying signatures on the receiving end
- Idempotency — handling duplicate deliveries safely
- Retry logic with exponential backoff
- Responding quickly and processing asynchronously

## Project Structure

```
sender.py      # Webhook sender — manages subscribers, signs & delivers webhooks
receiver.py    # Webhook receiver — verifies signatures, deduplicates, processes events
trigger.py     # Trigger script — runs the full flow end to end
```

## Prerequisites

- Python 3.12+
- pip

## Setup

```bash
# Clone the repo
git clone https://github.com/Ming0217/learn_webhook.git
cd learn_webhook

# Install dependencies
pip install -e .
```

## Running the Demo

You'll need three terminal windows.

**Terminal 1 — Start the sender (port 8000):**

```bash
uvicorn sender:app --port 8000 --reload
```

**Terminal 2 — Start the receiver (port 9000):**

```bash
uvicorn receiver:app --port 9000 --reload
```

**Terminal 3 — Run the trigger script:**

```bash
python trigger.py
```

The trigger script will:

1. Register the receiver as a webhook subscriber on the sender
2. Fire an `order.created` event
3. Fire an `order.shipped` event
4. Query the receiver to confirm it received both events
5. Print the sender's delivery log

## Interactive API Docs

Both servers come with auto-generated Swagger docs (courtesy of FastAPI):

- Sender: [http://localhost:8000/docs](http://localhost:8000/docs)
- Receiver: [http://localhost:9000/docs](http://localhost:9000/docs)

You can use these to manually fire events, register subscribers, and inspect state.

## Experiments to Try

Once you've seen the basic flow, try these to deepen your understanding:

| Experiment | What you'll learn |
|---|---|
| Change `WEBHOOK_SECRET` in `receiver.py` to a wrong value | Signature verification and why it matters |
| Stop the receiver, then fire an event from the sender | Retry logic and exponential backoff |
| Fire the same event twice with the same webhook ID | Idempotency and duplicate detection |
| Register multiple receivers and fire one event | Fan-out delivery to multiple subscribers |
| Add a new event type (e.g., `order.cancelled`) | How event filtering works |

## Suggested Reading Order

1. **`trigger.py`** — the simplest file, shows the big picture
2. **`receiver.py`** — "your" side of the webhook, where events arrive
3. **`sender.py`** — the provider side, the most complex piece

## Tech Stack

- [FastAPI](https://fastapi.tiangolo.com/) — web framework
- [Uvicorn](https://www.uvicorn.org/) — ASGI server
- [httpx](https://www.python-httpx.org/) — async HTTP client

## License

MIT
