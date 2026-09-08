import requests
from odoo_connections import get_whatsapp_config


def download_media(media_id, phone_number_id, extension="ogg"):
    config = get_whatsapp_config(phone_number_id)
    token = config["whatsapp_token"]
    api_version = config.get("graph_api_version", "v23.0")

    meta_url = f"https://graph.facebook.com/{api_version}/{media_id}"
    headers = {"Authorization": f"Bearer {token}"}

    meta_response = requests.get(meta_url, headers=headers)
    meta_response.raise_for_status()
    media_url = meta_response.json()["url"]

    file_response = requests.get(media_url, headers=headers)
    file_response.raise_for_status()

    file_path = f"/tmp/{media_id}.{extension}"
    with open(file_path, "wb") as f:
        f.write(file_response.content)

    return file_path