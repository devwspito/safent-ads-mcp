"""Explicit two-request owner confirmation for fixture clients, never production."""

import httpx


async def confirmed_request(client: httpx.AsyncClient, method: str, path: str, **kwargs):
    response = await client.request(method, path, **kwargs)
    # Auth/scope/precondition failures must retain their original behavior.
    if response.status_code != 428 or response.json()["error"]["code"] != "CONFIRMATION_REQUIRED":
        return response
    token = response.json()["error"]["details"]["confirmation_token"]
    headers = {**kwargs.pop("headers", {}), "X-Action-Confirmation": token}
    return await client.request(method, path, headers=headers, **kwargs)
