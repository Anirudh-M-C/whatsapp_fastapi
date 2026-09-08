# WhatsApp FastAPI

FastAPI service that connects the Odoo WhatsApp system with the Meta WhatsApp Cloud API.

## Features

* Meta WhatsApp Cloud API integration
* WhatsApp webhooks
* Incoming message processing
* Outgoing WhatsApp messages
* AI customer support
* Odoo integration
* Conversation memory
* Human takeover
* WhatsApp notifications
* Broadcast messaging
* Payment workflows
* Media processing
* PDF/document processing
* RAG and knowledge retrieval
* Internal Odoo communication

## Architecture

```text
WhatsApp Customer
       ↓
Meta WhatsApp Cloud API
       ↓
FastAPI
       ├── AI Agent
       ├── WhatsApp Sender
       ├── Odoo Connection
       ├── Conversation Store
       ├── Media Processing
       └── Payment Integration
       ↓
Odoo
```

## Main Components

### `llm_test.py`

AI agent and message-processing logic.

### `whatsapp_sender.py`

Handles outgoing WhatsApp messages through the Meta Cloud API.

### `whatsapp_media.py`

Handles WhatsApp media processing.

### `odoo_connections.py`

Handles communication with Odoo.

### `conversation_store.py`

Handles conversation-related local storage.

### `human_takeover.py`

Handles employee/human takeover functionality.

### `notifications.py`

Handles WhatsApp notification functionality.

### `payments.py`

Handles payment-related workflows.

### `rag.py`

Handles knowledge retrieval and RAG functionality.

### `rate_limiter.py`

Handles request rate limiting.

## Odoo Integration

FastAPI communicates with the Odoo WhatsApp module for:

* Products
* Stock
* Customers
* Sales orders
* Conversations
* AI configuration
* Notification templates
* Broadcast campaigns
* Payment workflows

## Local Setup

Create a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Configure environment variables in `.env`.

Never commit `.env`.

## Security

The following files must not be committed:

```text
.env
*.db
*.log
__pycache__/
```

Production credentials should always be stored securely.

## Status

Core WhatsApp AI, Meta integration, Odoo integration, conversation handling, human takeover, RAG, media/document processing, notifications, broadcasts, and payment-link workflows are implemented.

Future improvements include:

* Image sending
* File attachments
* Emoji support
* Additional WhatsApp media types
* Production deployment

## License

Private project.
