"""The stock ledger. Not built yet — this is Phase 1 and 2 work.

Two rules from AsOne will shape everything in this file, and both are easy to
break by accident:

1. **Inventory is an append-only ledger, not a counter.** Every movement is a
   permanent row with a user attached. A stock level is derived by summing
   the ledger, never by updating a quantity in place. Overwriting a number
   destroys the audit trail AsOne asked for (p.9).

2. **Every transaction records the user.** No shared logins, no generic
   accounts. The user comes from the request, never from a form field.

The columns AsOne listed for this table (p.5, p.6):

    Transaction #, Document #, Warehouse, Date, SKU #, Quantity,
    Stock Location (?), Source, Destination, Transaction Type,
    Inventory Value, User Name

"Stock Location" carries a question mark in their own document — whether bin
locations are in scope is open question Q9.

Open question Q1 also lands here: does stock leave inventory at pick time or
ship time? Their chart says "Shipped ???". The answer decides whether the
ledger records one movement or two.
"""
