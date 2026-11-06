"""Northstar Labs IT access-request intake.

Turns exported free-text access requests into records that conform to snapshot.schema.json and
routes them to a human review queue under the office policy. Every model call goes through
access_intake.llm.ModelClient, so the provider can be chosen later; tests use MockModelClient.
"""

__version__ = "0.1.0"
