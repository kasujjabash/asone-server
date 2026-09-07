Read-only aggregation over the other apps — nothing here has a model.

The dashboard asks questions that span apps: how much stock is on hand
(inventory), what is waiting to be picked (orders), what arrived short
(procurement). Putting those queries in any one of those apps would make it
depend on the other two.

So this app owns no data and writes nothing. It imports from the others and
adds up. If a figure here disagrees with the screen it came from, the other
screen is right and this one has a bug.
