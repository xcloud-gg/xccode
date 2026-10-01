"""xcroute: the one-host planner and hub in front of OmniRoute (XC-DES-001 §6.4.4, §6.5).

This package is the decision core. It has no network code: providers and the Jev decider are
injected, so every step is testable offline with mock providers.
"""
