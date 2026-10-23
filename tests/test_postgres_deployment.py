"""Offline checks that production database selection fails closed."""
import os
import unittest
from unittest.mock import patch

from postgres_legacy import AINAV_DATABASE_ID, transport_for


class PostgresDeploymentTests(unittest.TestCase):
    def test_postgres_backend_requires_connection_instead_of_falling_back(self):
        with patch.dict(os.environ, {'DATABASE_BACKEND': 'postgres', 'DATABASE_URL': ''}):
            with self.assertRaisesRegex(ValueError, 'no fallback to D1'):
                transport_for(AINAV_DATABASE_ID)

    def test_postgres_backend_rejects_other_databases(self):
        with patch.dict(os.environ, {'DATABASE_BACKEND': 'postgres'}):
            with self.assertRaisesRegex(ValueError, 'scoped to ainav'):
                transport_for('other-database')

    def test_postgres_backend_selects_pg_transport(self):
        url = 'postgresql://ci:ci@postgres.invalid/sigpik?sslmode=verify-full'
        with patch.dict(os.environ, {'DATABASE_BACKEND': 'postgres', 'DATABASE_URL': url}), patch('postgres_legacy.LegacyPostgresClient') as client:
            self.assertIs(transport_for(AINAV_DATABASE_ID), client.return_value)
            client.assert_called_once_with(url)
