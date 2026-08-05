"""Read account balances from the derived view.

Contract: SELECTs from account_balances only. There is no stored balance column
anywhere in this system; if this module ever writes, that is a bug by
definition.
"""
