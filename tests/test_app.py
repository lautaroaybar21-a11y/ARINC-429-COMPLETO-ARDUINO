import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import Instrument, Shipment, _transmit, create_app, db


class AppCrudTests(unittest.TestCase):
    def setUp(self):
        self.settings_directory = TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "WTF_CSRF_ENABLED": False,
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "SERIAL_PORT": "",
                "OUTPUT_VOLTAGE_MIN": Decimal("0.5"),
                "OUTPUT_VOLTAGE_MAX": Decimal("4.5"),
                "SETTINGS_ENV_FILE": Path(self.settings_directory.name) / ".env",
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()
        self.settings_directory.cleanup()

    def create_instrument(self):
        response = self.client.post(
            "/instruments/new",
            data={
                "name": "Altímetro",
                "description": "Prueba",
                "value_min": "0",
                "value_max": "100",
                "label_octal": "213",
                "sdi": "1",
                "ssm": "0",
                "encoding": "BNR",
                "resolution": "0.1",
            },
        )
        self.assertEqual(response.status_code, 302)
        return db.session.query(Instrument).one()

    def test_instrument_and_shipment_crud_and_generated_word(self):
        instrument = self.create_instrument()
        self.assertEqual(self.client.get("/instruments").status_code, 200)
        self.assertEqual(self.client.get("/shipments/new").status_code, 200)

        response = self.client.post(
            f"/instruments/{instrument.id}/edit",
            data={
                "name": "Altímetro actualizado",
                "description": "Configuración revisada",
                "value_min": "0",
                "value_max": "100",
                "label_octal": "214",
                "sdi": "1",
                "ssm": "0",
                "encoding": "BNR",
                "resolution": "0.1",
            },
        )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(instrument)
        self.assertEqual(instrument.name, "Altímetro actualizado")

        response = self.client.post("/shipments/new", data={"instrument_id": instrument.id, "value": "50"})
        self.assertEqual(response.status_code, 302)
        shipment = db.session.query(Shipment).one()
        self.assertEqual(len(shipment.word_binary), 32)
        self.assertEqual(shipment.word_uint.bit_count() % 2, 1)
        self.assertEqual(shipment.calculated_voltage, Decimal("2.5000"))
        self.assertIn("no está configurado", shipment.serial_status)
        self.assertIsNone(shipment.transmitted_at)

        page = self.client.get("/shipments")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"0x", page.data)
        self.assertIn(b"Sin salida f\xc3\xadsica", page.data)

        response = self.client.post(
            f"/shipments/{shipment.id}/edit",
            data={"instrument_id": instrument.id, "value": "25"},
        )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(shipment)
        self.assertEqual(shipment.value, Decimal("25.0000"))
        self.assertEqual(shipment.serial_status, "Modificado; pendiente de transmisión")

        self.client.post(f"/shipments/{shipment.id}/delete")
        self.assertEqual(db.session.query(Shipment).count(), 0)
        self.client.post(f"/instruments/{instrument.id}/delete")
        self.assertEqual(db.session.query(Instrument).count(), 0)

    def test_invalid_value_is_rejected_and_instrument_with_shipments_is_protected(self):
        instrument = self.create_instrument()
        response = self.client.post("/shipments/new", data={"instrument_id": instrument.id, "value": "101"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.query(Shipment).count(), 0)

        self.client.post("/shipments/new", data={"instrument_id": instrument.id, "value": "30"})
        response = self.client.post(f"/instruments/{instrument.id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.query(Instrument).count(), 1)
        self.assertEqual(db.session.query(Shipment).count(), 1)

    def test_serial_transmits_exact_configured_four_bytes(self):
        instrument = self.create_instrument()
        shipment = Shipment(
            instrument=instrument,
            value=Decimal("50"),
            word_uint=0x12345678,
            serial_status="Pendiente",
            voltage_status="No configurada",
        )
        db.session.add(shipment)
        db.session.commit()
        self.app.config.update(SERIAL_PORT="COM_TEST", SERIAL_BYTE_ORDER="big")

        with patch("serial.Serial") as serial_class:
            serial_class.return_value.__enter__.return_value.write.return_value = 4
            _transmit(self.app, shipment)

        serial_class.return_value.__enter__.return_value.write.assert_called_once_with(
            bytes.fromhex("12345678")
        )
        self.assertIsNotNone(shipment.transmitted_at)
        self.assertIn("COM_TEST", shipment.serial_status)

    def test_serial_refuses_to_guess_byte_order(self):
        instrument = self.create_instrument()
        shipment = Shipment(
            instrument=instrument,
            value=Decimal("50"),
            word_uint=0x12345678,
            serial_status="Pendiente",
            voltage_status="No configurada",
        )
        self.app.config.update(SERIAL_PORT="COM_TEST", SERIAL_BYTE_ORDER="")
        _transmit(self.app, shipment)
        self.assertIn("configurar SERIAL_BYTE_ORDER", shipment.serial_status)
        self.assertIsNone(shipment.transmitted_at)

    def test_instrument_edit_rejects_range_that_invalidates_history(self):
        instrument = self.create_instrument()
        self.client.post("/shipments/new", data={"instrument_id": instrument.id, "value": "80"})
        response = self.client.post(
            f"/instruments/{instrument.id}/edit",
            data={
                "name": "Altímetro",
                "description": "Prueba",
                "value_min": "0",
                "value_max": "70",
                "label_octal": "213",
                "sdi": "1",
                "ssm": "0",
                "encoding": "BNR",
                "resolution": "0.1",
            },
        )
        self.assertEqual(response.status_code, 200)
        db.session.refresh(instrument)
        self.assertEqual(instrument.value_max, Decimal("100.0000"))

    def test_bcd_instrument_rejects_fractional_range(self):
        response = self.client.post(
            "/instruments/new",
            data={
                "name": "Contador BCD",
                "value_min": "0.5",
                "value_max": "9999",
                "label_octal": "213",
                "sdi": "0",
                "ssm": "0",
                "encoding": "BCD",
                "resolution": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.query(Instrument).count(), 0)
        self.assertIn("límites".encode(), response.data)

    def test_hardware_settings_save_to_config_without_guessing_bytes(self):
        response = self.client.get("/hardware")
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/hardware",
            data={
                "serial_port": "COM_TEST",
                "serial_baudrate": "57600",
                "serial_byte_order": "little",
                "voltage_min": "0.25",
                "voltage_max": "4.75",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.app.config["SERIAL_PORT"], "COM_TEST")
        self.assertEqual(self.app.config["SERIAL_BAUDRATE"], 57600)
        self.assertEqual(self.app.config["SERIAL_BYTE_ORDER"], "little")
        self.assertEqual(self.app.config["OUTPUT_VOLTAGE_MIN"], Decimal("0.25"))
        saved = self.app.config["SETTINGS_ENV_FILE"].read_text(encoding="utf-8")
        self.assertIn("SERIAL_PORT='COM_TEST'", saved)

    def test_hardware_settings_reject_port_without_byte_order(self):
        response = self.client.post(
            "/hardware",
            data={
                "serial_port": "COM_TEST",
                "serial_baudrate": "115200",
                "serial_byte_order": "",
                "voltage_min": "",
                "voltage_max": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("orden de bytes".encode(), response.data)
        self.assertEqual(self.app.config["SERIAL_PORT"], "")

    def test_instrument_name_is_free_text(self):
        response = self.client.get("/instruments/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="name"'.encode(), response.data)
        self.assertNotIn("Plantillas de instrumentos".encode(), response.data)
        self.assertNotIn("Altímetro".encode(), response.data)

        response = self.client.post(
            "/instruments/new",
            data={
                "name": "Mi instrumento personalizado 777",
                "description": "Nombre libre",
                "value_min": "0",
                "value_max": "100",
                "label_octal": "213",
                "sdi": "0",
                "ssm": "0",
                "encoding": "BNR",
                "resolution": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(db.session.query(Instrument).one().name, "Mi instrumento personalizado 777")

    def test_custom_instrument_created_from_shipment_returns_selected(self):
        response = self.client.post(
            "/instruments/new?source=shipment",
            data={
                "name": "A elección del usuario",
                "value_min": "-10",
                "value_max": "10",
                "label_octal": "123",
                "sdi": "0",
                "ssm": "0",
                "encoding": "BNR",
                "resolution": "0.1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/shipments/new?instrument_id=1", response.location)
        page = self.client.get(response.location)
        self.assertEqual(page.status_code, 200)
        self.assertIn('value="1" data-min="-10.0000"'.encode(), page.data)

    def test_arinc_preview_requires_complete_valid_instrument_and_value(self):
        instrument = self.create_instrument()
        response = self.client.post(
            "/api/arinc-preview",
            json={"instrument_id": instrument.id, "value": "50"},
        )
        self.assertEqual(response.status_code, 200)
        preview = response.get_json()
        self.assertEqual(preview["word_hex"], "0x0007D1D1")
        self.assertEqual(len(preview["word_binary"]), 32)
        self.assertEqual(db.session.query(Shipment).count(), 0)

        response = self.client.post(
            "/api/arinc-preview",
            json={"instrument_id": instrument.id, "value": "101"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("valor debe estar".encode(), response.data)

    def test_shipment_form_explains_registered_instrument_list(self):
        self.create_instrument()
        response = self.client.get("/shipments/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn("1 instrumento disponible en MySQL".encode(), response.data)
        self.assertIn("Agregar instrumento".encode(), response.data)


if __name__ == "__main__":
    unittest.main()