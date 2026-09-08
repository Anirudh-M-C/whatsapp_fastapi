import requests
from io import BytesIO
from odoo_connections import get_whatsapp_config

try:
    from PIL import Image
except ImportError:
    Image = None


def send_text_message(to, text, phone_number_id):
    config = get_whatsapp_config(phone_number_id)
    token = config["whatsapp_token"]
    api_version = config.get("graph_api_version", "v23.0")

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    response = requests.post(url, headers=headers, json=data)
    print(response.status_code, response.json())
    return response.json()


def _guess_image_filename_and_mime(image_bytes):
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "product.png", "image/png"
    return "product.jpg", "image/jpeg"


def _normalize_image_for_whatsapp(image_bytes):
    """Odoo's stored bytes aren't always a clean, WhatsApp-accepted file (can
    be an unusual color mode, a different format than the extension implies,
    or slightly malformed). Decode with Pillow and re-encode as a fresh PNG
    so what we upload is always guaranteed-valid, regardless of source.
    Falls back to the raw bytes + best-guess mime if Pillow isn't installed
    or can't read the file (so an image still gets attempted either way)."""
    if Image is None:
        filename, mime_type = _guess_image_filename_and_mime(image_bytes)
        return image_bytes, filename, mime_type

    try:
        img = Image.open(BytesIO(image_bytes))
        img.load()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue(), "product.png", "image/png"
    except Exception as e:
        print(f"Image normalization failed, sending raw bytes as fallback: {e!r}")
        filename, mime_type = _guess_image_filename_and_mime(image_bytes)
        return image_bytes, filename, mime_type


def send_image_bytes_message(to, image_bytes, phone_number_id, caption=None):
    """Sends an image we already have as raw bytes (e.g. straight from Odoo),
    rather than a public URL. Uploads to WhatsApp's Media endpoint to get a
    media_id, then sends by media_id — same token/config/Graph API as every
    other sender here, just skips the link-fetch step send_image_message needs."""
    config = get_whatsapp_config(phone_number_id)
    token = config["whatsapp_token"]
    api_version = config.get("graph_api_version", "v23.0")
    image_bytes, filename, mime_type = _normalize_image_for_whatsapp(image_bytes)

    upload_url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/media"
    headers = {"Authorization": f"Bearer {token}"}
    files = {"file": (filename, image_bytes, mime_type)}
    data = {"messaging_product": "whatsapp"}

    upload_response = requests.post(upload_url, headers=headers, data=data, files=files)
    upload_data = upload_response.json()
    media_id = upload_data.get("id")
    if not media_id:
        print(upload_response.status_code, upload_data)
        return upload_data

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    image_payload = {"id": media_id}
    if caption:
        image_payload["caption"] = caption
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": image_payload,
    }
    response = requests.post(url, headers=headers, json=data)
    print(response.status_code, response.json())
    return response.json()


def send_image_message(to, image_url, phone_number_id, caption=None):
    """Sends a standalone image message. Used by the broadcast sender to show
    a template's image header when the recipient is inside the 24h session
    window (free-text path has no header concept, so the image is sent as a
    preceding message instead)."""
    config = get_whatsapp_config(phone_number_id)
    token = config["whatsapp_token"]
    api_version = config.get("graph_api_version", "v23.0")

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    image_payload = {"link": image_url}
    if caption:
        image_payload["caption"] = caption
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": image_payload,
    }
    response = requests.post(url, headers=headers, json=data)
    print(response.status_code, response.json())
    return response.json()


def send_template_message(to, template_name, language_code, body_params, phone_number_id,
                           header_image_url=None):
    """Sends a Meta-approved template message. Required for broadcasts to
    recipients outside the 24h session window. body_params is an ordered list
    of strings mapped positionally onto the template's {{1}}, {{2}}, ...
    variables (see whatsapp.notification.template._get_variable_order)."""
    config = get_whatsapp_config(phone_number_id)
    token = config["whatsapp_token"]
    api_version = config.get("graph_api_version", "v23.0")

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    components = []
    if header_image_url:
        components.append({
            "type": "header",
            "parameters": [{"type": "image", "image": {"link": header_image_url}}],
        })
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        })

    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code or "en_US"},
        },
    }
    if components:
        data["template"]["components"] = components

    response = requests.post(url, headers=headers, json=data)
    print(response.status_code, response.json())
    return response.json()

def send_document_bytes_message(to, doc_bytes, phone_number_id, filename="invoice.pdf", caption=None):
    config = get_whatsapp_config(phone_number_id)
    token = config["whatsapp_token"]
    api_version = config.get("graph_api_version", "v23.0")

    upload_url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/media"
    headers = {"Authorization": f"Bearer {token}"}
    files = {"file": (filename, doc_bytes, "application/pdf")}
    data = {"messaging_product": "whatsapp"}

    upload_response = requests.post(upload_url, headers=headers, data=data, files=files)
    upload_data = upload_response.json()
    media_id = upload_data.get("id")
    if not media_id:
        print(upload_response.status_code, upload_data)
        return upload_data

    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    doc_payload = {"id": media_id, "filename": filename}
    if caption:
        doc_payload["caption"] = caption
    data = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": doc_payload,
    }
    response = requests.post(url, headers=headers, json=data)
    print(response.status_code, response.json())
    return response.json()