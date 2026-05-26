"""Structured error helper for the agent REST API SDK."""
from fastapi import HTTPException


def error(status: int, detail: str) -> HTTPException:
    """
    Return a FastAPI ``HTTPException`` with a consistent JSON error body.

    Raise it from a handler to produce a structured error response::

        from cinna_api import api, error

        @api.get("/orders/{order_id}")
        def get_order(order_id: int):
            order = lookup(order_id)
            if order is None:
                raise error(404, "Order not found")
            return order
    """
    return HTTPException(status_code=status, detail=detail)
