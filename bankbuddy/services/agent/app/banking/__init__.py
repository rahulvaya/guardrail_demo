"""Banking client adapters.

The agent depends on the abstract `IBankingService` from `bankbuddy_shared`.
The factory chooses an implementation at startup based on `BANKING_BACKEND`.
"""
