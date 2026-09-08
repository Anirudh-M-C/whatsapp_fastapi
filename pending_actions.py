pending_actions = {}


def set_pending(user_id, action):
    pending_actions[user_id] = action


def get_pending(user_id):
    return pending_actions.get(user_id)


def clear_pending(user_id):
    pending_actions.pop(user_id, None)