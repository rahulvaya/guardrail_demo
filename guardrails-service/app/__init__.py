"""Guardrails Service - internal policy enforcement API.

A standalone HTTP service that evaluates input and output text against
configurable guard pipelines. Consumers integrate over plain HTTP
(``POST /v1/check``) and never depend on this package directly.
"""
