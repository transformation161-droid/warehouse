import os
import unittest
from tests import test_writeoff_analytics as analytics,test_postgres as pg


@unittest.skipUnless(os.getenv('KSU_TEST_PG_ADMIN_DSN'),'Real local PostgreSQL required')
class PostgreSQLAnalytics(analytics.Analytics):
    setUp=pg.PostgreSQL.setUp
    tearDown=pg.PostgreSQL.tearDown
