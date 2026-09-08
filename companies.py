COMPANIES = {
    "1271850492686057": {
        "name": "Default Store",
        "odoo_url": "http://localhost:8076",
        "odoo_db": "db12345",
        "odoo_username": "anirudhm394@gmail.com",
        "odoo_api_key_env": "ODOO_API_KEY",
    },
}


def get_company(phone_number_id):
    return COMPANIES.get(phone_number_id)