"""Controllers: the orchestration layer between routes and services.

Controllers receive already-extracted primitives from the route (session
mapping, query values, DB session) and call the matching service. They hold no
business rules of their own.
"""
