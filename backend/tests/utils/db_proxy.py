"""Non-closing session proxy for test transaction isolation.

Wraps the test DB session so that service code using
`with create_session() as db:` doesn't close the underlying session.
The test fixture handles rollback after each test.
"""


class NonClosingSessionProxy:
    """Proxy that suppresses session close on context-manager exit.

    Service code does::

        with create_session() as fresh_db:
            ...

    The context manager ``__exit__`` would normally close the session.
    This proxy suppresses that so everything stays on the test transaction.
    """

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self._session

    def __exit__(self, *args):
        pass  # Don't close — the test fixture handles rollback

    def __getattr__(self, name):
        return getattr(self._session, name)
