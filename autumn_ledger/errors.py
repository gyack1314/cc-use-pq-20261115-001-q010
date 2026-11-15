class LedgerError(Exception):
    def __init__(self, code, message, status=422, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


def require(condition, code, message, status=422, details=None):
    if not condition:
        raise LedgerError(code, message, status, details)


def missing(kind, identity):
    return LedgerError("not_found", f"找不到{kind}：{identity}", 404)


def conflict(message, details=None):
    return LedgerError("conflict", message, 409, details)
