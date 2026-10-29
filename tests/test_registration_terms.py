import unittest
from unittest.mock import patch

import mysql.connector.pooling
from fastapi import HTTPException
from pydantic import ValidationError


class _PoolWithoutDatabase:
    def __init__(self, *args, **kwargs):
        pass


with patch.object(mysql.connector.pooling, "MySQLConnectionPool", _PoolWithoutDatabase):
    import main


class RegistrationTermsTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "name": "Terms Test",
            "email": "terms-test@example.com",
            "password": "password123",
            "role": "consumer",
        }

    def test_registration_requires_terms_field(self):
        with self.assertRaises(ValidationError):
            main.Register(**self.payload)

    def test_registration_rejects_unaccepted_terms_before_database_access(self):
        data = main.Register(**self.payload, terms_accepted=False)
        with patch.object(main, "q") as query, patch.object(main, "exec_sql") as insert:
            with self.assertRaises(HTTPException) as error:
                main.register(data, None, object())

        self.assertEqual(error.exception.status_code, 400)
        self.assertIn("Terms and Conditions", error.exception.detail)
        query.assert_not_called()
        insert.assert_not_called()

    def test_registration_accepts_true_terms_value_in_request_model(self):
        data = main.Register(**self.payload, terms_accepted=True)
        self.assertTrue(data.terms_accepted)


if __name__ == "__main__":
    unittest.main()
