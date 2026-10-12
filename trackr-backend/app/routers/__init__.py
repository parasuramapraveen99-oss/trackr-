"""FastAPI controllers, aligned to contracts/openapi.yaml's path list. Each router does
the same three things every route in the old api/main.py did -- load the ledger, call an
engine function, serialise the result -- and nothing here computes anything the engine
did not already compute. See app/main.py for where these are registered.
"""
