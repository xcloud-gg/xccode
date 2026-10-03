"""xcroute: the one-host planner and hub in front of OmniRoute (XC-CODE-001 §4.4, §4.5).

This package is the decision core. It has no network code: the provider is injected, so every
step is testable offline with mock providers.
"""
