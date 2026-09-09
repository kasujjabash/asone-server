"""Pagination that lets a caller choose its page size.

Stock `PageNumberPagination` fixes the size at `PAGE_SIZE` and ignores any
request for a different one, which forces a client into a choice between two
bad options: render fifty rows where the design shows ten, or fetch fifty and
slice — after which the page numbers no longer describe what is on screen.

`max_page_size` is the reason this is a class rather than one setting. Without
a ceiling, `?page_size=100000` turns any list endpoint into a way to pull the
whole table in one request, which is a denial-of-service footgun on the
movement ledger in particular.
"""

from rest_framework.pagination import PageNumberPagination


class SizedPageNumberPagination(PageNumberPagination):
    #: The default when a caller does not ask, unchanged from before.
    page_size = 50

    #: What a caller sends to ask for something else: ?page_size=10
    page_size_query_param = "page_size"

    #: A ceiling, so no caller can ask for the whole table at once.
    max_page_size = 200
