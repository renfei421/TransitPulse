def success(data):
    return {"status": "ok", "data": data}

def error(msg, status_code=400):
    return {"status": "error", "message": msg}