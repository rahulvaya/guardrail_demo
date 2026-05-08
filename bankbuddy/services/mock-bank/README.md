# mock-bank service

Stand-in for a real core-banking system. Lives on the **internal** Docker network only - no host port mapping.

## Surface

| Method | Path                                          | Purpose                                    |
| ------ | --------------------------------------------- | ------------------------------------------ |
| GET    | `/health`                                     | Liveness                                   |
| GET    | `/accounts`                                   | List accounts for `X-User-Id`              |
| GET    | `/accounts/{id}/transactions`                 | List transactions for an owned account     |
| POST   | `/transfers`                                  | Internal transfer between owned accounts   |
| GET    | `/cards`                                      | List cards for `X-User-Id`                 |
| POST   | `/cards/{id}/block`                           | Block a card                               |
| GET    | `/atms?postal_code=&radius_km=`               | Find nearby ATMs                           |
| POST   | `/loans/eligibility`                          | Deterministic eligibility check            |

All authenticated routes require the `X-User-Id` header. The agent injects this header from the verified `Principal.subject`.

## Schema

Owned by `bank_user` in the `bank` schema:

- `customers` - demo customers (`cust-alice`, `cust-bob`)
- `accounts` - checking/savings, decimal balances
- `transactions` - posted entries; positive = credit, negative = debit
- `cards` - VISA / MASTERCARD cards with blocked flag
- `atms` - geo-tagged ATMs with postal-code prefix lookup

`SQLAlchemy 2.0 ORM` declarative models in [app/models.py](app/models.py); tables are created on startup via `Base.metadata.create_all`. Demo data is inserted idempotently by [app/seed.py](app/seed.py) when the customers table is empty.

## Replacing with a real backend

Swap this service for a real core-banking API by:

1. Implementing a new `IBankingService` adapter in `services/agent/app/banking/`.
2. Setting `BANKING_BACKEND=real` and providing the new service URL.

The agent's tools, the API gateway, and the UI never reference mock-bank directly - they only see the `IBankingService` interface from `bankbuddy_shared.interfaces`.

## Design principles in play

- **Repository pattern** - SQL access lives in [app/repositories.py](app/repositories.py); routers stay thin.
- **DTO segregation** - request/response shapes in [app/schemas.py](app/schemas.py) decouple ORM from wire.
- **Twelve-factor config** - everything via env vars in [app/settings.py](app/settings.py).
- **Network isolation** - service is reachable only from the `internal` compose network.
- **Idempotent seed** - safe to restart; production-style migrations would arrive in Phase 1f.
