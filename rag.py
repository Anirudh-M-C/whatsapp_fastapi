from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from odoo_connections import get_odoo_connection

_indexes = {}


def _get_return_policy(conn, category_name):
    return conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.return.policy", "get_policy_for_category",
        [category_name]
    )


def _build_index(phone_number_id):
    conn = get_odoo_connection(phone_number_id)
    products = conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.bridge", "get_all_product_info", []
    )
    faq = conn["models"].execute_kw(
        conn["database"], conn["uid"], conn["api_key"],
        "whatsapp.knowledge.article", "get_all_articles", []
    )

    documents = []
    sources = []

    for entry in faq:
        documents.append(entry["question"] + " " + entry["answer"])
        sources.append(entry["answer"])

    for p in products:
        category = p.get("category", "")
        return_policy = _get_return_policy(conn, category)

        documents.append(f"{p['name']} {p.get('description', '')} return policy {category}")
        sources.append(
            f"{p['name']}: {p.get('description') or 'No description available.'} "
            f"Return policy: {return_policy}"
        )

    vectorizer = TfidfVectorizer()
    matrix = vectorizer.fit_transform(documents)

    return {"vectorizer": vectorizer, "matrix": matrix, "sources": sources}


from odoo_connections import get_ai_config


def search_knowledge_base(query, phone_number_id=None, top_k=None, score_threshold=None):
    if top_k is None or score_threshold is None:
        ai_config = get_ai_config(phone_number_id)
        if top_k is None:
            top_k = ai_config["rag_top_k"]
        if score_threshold is None:
            score_threshold = ai_config["rag_score_threshold"]

    if phone_number_id not in _indexes:
        _indexes[phone_number_id] = _build_index(phone_number_id)

    index = _indexes[phone_number_id]
    query_vec = index["vectorizer"].transform([query])
    scores = cosine_similarity(query_vec, index["matrix"])[0]

    ranked = sorted(zip(scores, index["sources"]), key=lambda x: x[0], reverse=True)
    results = [text for score, text in ranked[:top_k] if score > score_threshold]

    if not results:
        return {"found": False}

    return {"found": True, "results": results}