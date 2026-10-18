
def quote(symbol: str, qty: int = 1, ctx=None):
    """Example custom business logic."""
    price = {"AAPL": 190.0, "MSFT": 410.0}.get(symbol.upper())
    if price is None:
        from portwright import SourceError
        raise SourceError("Unknown symbol", 404)
    return {"symbol": symbol.upper(), "qty": qty, "total": price * qty, "caller": ctx.principal.subject}
