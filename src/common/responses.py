from typing import Any


def success_response(code: int = 200, message: str = "Success", data: Any = None) -> dict:
    return {
        "status": "success",
        "code": code,
        "message": message,
        "data": data,
    }


def error_response(code: int = 400, message: str = "Error", errors: Any = None) -> dict:
    return {
        "status": "error",
        "code": code,
        "message": message,
        "errors": errors,
    }
