import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import select
from dotenv import load_dotenv, set_key

from arinc import encode_word, scale_voltage, word_bits, word_bytes


load_dotenv(Path(__file__).with_name(".env"))

db = SQLAlchemy()
csrf = CSRFProtect()


class Instrument(db.Model):
    __tablename__ = "instrumentos"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    description = db.Column(db.Text)
    value_min = db.Column(db.Numeric(14, 4), nullable=False)
    value_max = db.Column(db.Numeric(14, 4), nullable=False)
    label_octal = db.Column(db.String(3), nullable=False)
    sdi = db.Column(db.SmallInteger, nullable=False, default=0)
    ssm = db.Column(db.SmallInteger, nullable=False, default=0)
    encoding = db.Column(db.String(8), nullable=False, default="BNR")
    resolution = db.Column(db.Numeric(14, 8), nullable=False, default=1)
    shipments = db.relationship("Shipment", back_populates="instrument", passive_deletes=True)


class Shipment(db.Model):
    __tablename__ = "envios"

    id = db.Column(db.Integer, primary_key=True)
    instrument_id = db.Column(
        db.Integer,
        db.ForeignKey("instrumentos.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    value = db.Column(db.Numeric(14, 4), nullable=False)
    word_uint = db.Column(db.BigInteger, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    transmitted_at = db.Column(db.DateTime(timezone=True))
    serial_status = db.Column(db.String(255), nullable=False, default="Pendiente")
    calculated_voltage = db.Column(db.Numeric(10, 4))
    voltage_status = db.Column(db.String(255), nullable=False)
    instrument = db.relationship("Instrument", back_populates="shipments")

    @property
    def word_binary(self):
        return word_bits(self.word_uint)

    @property
    def word_hex(self):
        return f"0x{self.word_uint:08X}"


def _serial_configured(app):
    return bool(app.config["SERIAL_PORT"])


def _transmit(app, shipment):
    if not _serial_configured(app):
        shipment.serial_status = "No transmitido: SERIAL_PORT no está configurado"
        return
    if not app.config["SERIAL_BYTE_ORDER"]:
        shipment.serial_status = "No transmitido: configurar SERIAL_BYTE_ORDER para el adaptador"
        return
    try:
        import serial

        payload = word_bytes(shipment.word_uint, app.config["SERIAL_BYTE_ORDER"])
        with serial.Serial(
            app.config["SERIAL_PORT"],
            app.config["SERIAL_BAUDRATE"],
            timeout=app.config["SERIAL_TIMEOUT"],
            write_timeout=app.config["SERIAL_TIMEOUT"],
        ) as port:
            written = port.write(payload)
            port.flush()
        if written != len(payload):
            shipment.serial_status = f"Error: se escribieron {written} de {len(payload)} bytes"
            return
        shipment.transmitted_at = datetime.now(timezone.utc)
        shipment.serial_status = (
            f"Enviado: 4 bytes {app.config['SERIAL_BYTE_ORDER']}-endian a "
            f"{app.config['SERIAL_PORT']}"
        )
    except Exception as error:
        shipment.serial_status = f"Error de puerto serie: {error}"[:255]


def _voltage_for(app, instrument, value):
    minimum = app.config["OUTPUT_VOLTAGE_MIN"]
    maximum = app.config["OUTPUT_VOLTAGE_MAX"]
    if minimum is None or maximum is None:
        return None, "No calculada: configurar OUTPUT_VOLTAGE_MIN/MAX"
    try:
        voltage = scale_voltage(value, instrument.value_min, instrument.value_max, minimum, maximum)
        return voltage, "Calculada; no existe salida física configurada"
    except ValueError as error:
        return None, f"No calculada: {error}"


def _shipment_values(form, app):
    try:
        instrument_id = int(form.get("instrument_id", ""))
        value = Decimal(form.get("value", ""))
    except (ValueError, InvalidOperation):
        raise ValueError("Seleccioná un instrumento y escribí un valor numérico válido.")
    if not value.is_finite():
        raise ValueError("El valor debe ser finito.")
    instrument = db.session.get(Instrument, instrument_id)
    if instrument is None:
        raise ValueError("El instrumento seleccionado no existe.")
    if value < instrument.value_min or value > instrument.value_max:
        raise ValueError(
            f"El valor debe estar entre {instrument.value_min} y {instrument.value_max} "
            f"para {instrument.name}."
        )
    word = encode_word(
        instrument.label_octal,
        instrument.sdi,
        instrument.ssm,
        value,
        instrument.encoding,
        instrument.resolution,
    )
    voltage, voltage_status = _voltage_for(app, instrument, value)
    return instrument, value, word, voltage, voltage_status


def _instrument_values(form):
    name = form.get("name", "").strip()
    if not name:
        raise ValueError("El nombre del instrumento es obligatorio.")
    try:
        value_min = Decimal(form.get("value_min", ""))
        value_max = Decimal(form.get("value_max", ""))
        resolution = Decimal(form.get("resolution", ""))
        sdi = int(form.get("sdi", ""))
        ssm = int(form.get("ssm", ""))
    except (ValueError, InvalidOperation):
        raise ValueError("Rangos, resolución, SDI y SSM deben tener valores numéricos válidos.")
    if not all(item.is_finite() for item in (value_min, value_max, resolution)):
        raise ValueError("Los valores del instrumento deben ser finitos.")
    if value_max <= value_min or resolution <= 0:
        raise ValueError("El máximo debe superar al mínimo y la resolución debe ser mayor que cero.")
    label = form.get("label_octal", "").strip()
    encoding = form.get("encoding", "").upper()
    try:
        encode_word(label, sdi, ssm, 0, encoding, resolution)
    except ValueError as error:
        raise ValueError(str(error))
    if encoding == "BCD" and (value_min < 0 or value_max > 9999):
        raise ValueError("El rango BCD debe estar entre 0 y 9999.")
    if encoding == "BCD" and (
        value_min != value_min.to_integral_value() or value_max != value_max.to_integral_value()
    ):
        raise ValueError("Los límites de un instrumento BCD deben ser valores enteros.")
    return {
        "name": name,
        "description": form.get("description", "").strip() or None,
        "value_min": value_min,
        "value_max": value_max,
        "label_octal": label,
        "sdi": sdi,
        "ssm": ssm,
        "encoding": encoding,
        "resolution": resolution,
    }


def _hardware_values(form):
    port = form.get("serial_port", "").strip()
    byte_order = form.get("serial_byte_order", "").lower()
    try:
        baudrate = int(form.get("serial_baudrate", ""))
    except ValueError:
        raise ValueError("La velocidad serie debe ser un número entero.")
    if not 1200 <= baudrate <= 2_000_000:
        raise ValueError("La velocidad serie debe estar entre 1200 y 2000000 baudios.")
    if byte_order not in ("", "little", "big"):
        raise ValueError("El orden de bytes debe ser little o big.")
    if port and not byte_order:
        raise ValueError("Elegí el orden de bytes que espera el adaptador antes de habilitar el puerto.")

    voltage_min_text = form.get("voltage_min", "").strip()
    voltage_max_text = form.get("voltage_max", "").strip()
    if bool(voltage_min_text) != bool(voltage_max_text):
        raise ValueError("Ingresá ambos límites de tensión o dejá los dos vacíos.")
    if voltage_min_text:
        try:
            voltage_min = Decimal(voltage_min_text)
            voltage_max = Decimal(voltage_max_text)
        except InvalidOperation:
            raise ValueError("Los límites de tensión deben ser numéricos.")
        if not voltage_min.is_finite() or not voltage_max.is_finite() or voltage_max <= voltage_min:
            raise ValueError("El límite máximo de tensión debe ser finito y mayor que el mínimo.")
    else:
        voltage_min = None
        voltage_max = None
    return {
        "SERIAL_PORT": port,
        "SERIAL_BAUDRATE": baudrate,
        "SERIAL_BYTE_ORDER": byte_order,
        "OUTPUT_VOLTAGE_MIN": voltage_min,
        "OUTPUT_VOLTAGE_MAX": voltage_max,
    }


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "change-this-development-key"),
        SQLALCHEMY_DATABASE_URI=os.getenv(
            "DATABASE_URL",
            "mysql+pymysql://arinc:arinc@localhost:3306/arinc429",
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SERIAL_PORT=os.getenv("SERIAL_PORT", ""),
        SERIAL_BAUDRATE=int(os.getenv("SERIAL_BAUDRATE", "115200")),
        SERIAL_TIMEOUT=float(os.getenv("SERIAL_TIMEOUT", "2")),
        SERIAL_BYTE_ORDER=os.getenv("SERIAL_BYTE_ORDER", "").lower(),
        OUTPUT_VOLTAGE_MIN=os.getenv("OUTPUT_VOLTAGE_MIN"),
        OUTPUT_VOLTAGE_MAX=os.getenv("OUTPUT_VOLTAGE_MAX"),
        SETTINGS_ENV_FILE=Path(__file__).with_name(".env"),
    )
    if app.config["OUTPUT_VOLTAGE_MIN"] not in (None, ""):
        app.config["OUTPUT_VOLTAGE_MIN"] = Decimal(app.config["OUTPUT_VOLTAGE_MIN"])
    else:
        app.config["OUTPUT_VOLTAGE_MIN"] = None
    if app.config["OUTPUT_VOLTAGE_MAX"] not in (None, ""):
        app.config["OUTPUT_VOLTAGE_MAX"] = Decimal(app.config["OUTPUT_VOLTAGE_MAX"])
    else:
        app.config["OUTPUT_VOLTAGE_MAX"] = None
    if test_config:
        app.config.update(test_config)
    if app.config["SERIAL_BYTE_ORDER"] not in ("", "little", "big"):
        raise ValueError("SERIAL_BYTE_ORDER debe ser 'little' o 'big'.")

    db.init_app(app)
    csrf.init_app(app)

    @app.get("/")
    def index():
        return redirect(url_for("shipments"))

    @app.route("/hardware", methods=["GET", "POST"])
    def hardware_settings():
        if request.method == "POST":
            try:
                values = _hardware_values(request.form)
                env_path = Path(app.config["SETTINGS_ENV_FILE"])
                env_path.parent.mkdir(parents=True, exist_ok=True)
                env_path.touch(exist_ok=True)
                for key, value in values.items():
                    set_key(str(env_path), key, "" if value is None else str(value))
                app.config.update(values)
                flash("Configuración de hardware guardada.", "success")
                return redirect(url_for("hardware_settings"))
            except ValueError as error:
                flash(str(error), "error")
            except OSError:
                flash("No se pudo guardar .env. Revisá los permisos del proyecto.", "error")

        try:
            from serial.tools import list_ports

            ports = list(list_ports.comports())
        except Exception:
            ports = []
        return render_template("hardware.html", ports=ports, settings=app.config)

    @app.get("/instruments")
    def instruments():
        rows = db.session.scalars(select(Instrument).order_by(Instrument.name)).all()
        return render_template("instruments.html", instruments=rows, editing=None)

    @app.route("/instruments/new", methods=["GET", "POST"])
    def create_instrument():
        if request.method == "POST":
            try:
                instrument = Instrument(**_instrument_values(request.form))
                db.session.add(instrument)
                db.session.commit()
                flash("Instrumento creado.", "success")
                if request.args.get("source") == "shipment":
                    return redirect(url_for("create_shipment", instrument_id=instrument.id))
                return redirect(url_for("instruments"))
            except ValueError as error:
                db.session.rollback()
                flash(str(error), "error")
            except Exception:
                db.session.rollback()
                flash("No se pudo crear el instrumento. Revisá si el nombre ya está registrado.", "error")
        return render_template("instrument_form.html", instrument=None)

    @app.route("/instruments/<int:instrument_id>/edit", methods=["GET", "POST"])
    def edit_instrument(instrument_id):
        instrument = db.get_or_404(Instrument, instrument_id)
        if request.method == "POST":
            try:
                values = _instrument_values(request.form)
                for shipment in instrument.shipments:
                    if shipment.value < values["value_min"] or shipment.value > values["value_max"]:
                        raise ValueError("El nuevo rango dejaría envíos existentes fuera del rango permitido.")
                for key, value in values.items():
                    setattr(instrument, key, value)
                db.session.commit()
                flash("Instrumento actualizado.", "success")
                return redirect(url_for("instruments"))
            except ValueError as error:
                db.session.rollback()
                flash(str(error), "error")
            except Exception:
                db.session.rollback()
                flash("No se pudo actualizar el instrumento. Revisá si el nombre ya está registrado.", "error")
        return render_template("instrument_form.html", instrument=instrument)

    @app.post("/instruments/<int:instrument_id>/delete")
    def delete_instrument(instrument_id):
        instrument = db.get_or_404(Instrument, instrument_id)
        if instrument.shipments:
            flash("No se puede eliminar: el instrumento tiene envíos asociados.", "error")
            return redirect(url_for("instruments"))
        db.session.delete(instrument)
        db.session.commit()
        flash("Instrumento eliminado.", "success")
        return redirect(url_for("instruments"))

    @app.get("/shipments")
    def shipments():
        rows = db.session.scalars(
            select(Shipment).order_by(Shipment.created_at.desc(), Shipment.id.desc())
        ).all()
        return render_template("shipments.html", shipments=rows)

    @app.route("/shipments/new", methods=["GET", "POST"])
    def create_shipment():
        instruments = db.session.scalars(select(Instrument).order_by(Instrument.name)).all()
        if request.method == "POST":
            try:
                instrument, value, word, voltage, voltage_status = _shipment_values(request.form, app)
                shipment = Shipment(
                    instrument=instrument,
                    value=value,
                    word_uint=word,
                    serial_status="Pendiente",
                    calculated_voltage=voltage,
                    voltage_status=voltage_status,
                )
                db.session.add(shipment)
                db.session.commit()
                _transmit(app, shipment)
                db.session.commit()
                flash("Envío creado; revisá su estado serie y el cálculo de tensión.", "success")
                return redirect(url_for("shipments"))
            except ValueError as error:
                db.session.rollback()
                flash(str(error), "error")
            except Exception:
                db.session.rollback()
                flash("No se pudo guardar el envío. Verificá la conexión con la base de datos.", "error")
        selected_instrument_id = (
            request.form.get("instrument_id", type=int)
            if request.method == "POST"
            else request.args.get("instrument_id", type=int)
        )
        return render_template(
            "shipment_form.html",
            shipment=None,
            instruments=instruments,
            selected_instrument_id=selected_instrument_id,
        )

    @app.post("/api/arinc-preview")
    def arinc_preview():
        try:
            payload = request.get_json(silent=True) or {}
            instrument, value, word, voltage, voltage_status = _shipment_values(payload, app)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        return jsonify(
            {
                "word_hex": f"0x{word:08X}",
                "word_binary": word_bits(word),
                "encoding": instrument.encoding,
                "value": str(value),
                "voltage": str(voltage) if voltage is not None else None,
                "voltage_status": voltage_status,
            }
        )

    @app.route("/shipments/<int:shipment_id>/edit", methods=["GET", "POST"])
    def edit_shipment(shipment_id):
        shipment = db.get_or_404(Shipment, shipment_id)
        instruments = db.session.scalars(select(Instrument).order_by(Instrument.name)).all()
        if request.method == "POST":
            try:
                instrument, value, word, voltage, voltage_status = _shipment_values(request.form, app)
                shipment.instrument = instrument
                shipment.value = value
                shipment.word_uint = word
                shipment.calculated_voltage = voltage
                shipment.voltage_status = voltage_status
                shipment.transmitted_at = None
                shipment.serial_status = "Modificado; pendiente de transmisión"
                db.session.commit()
                flash("Envío actualizado. No se retransmitió automáticamente.", "success")
                return redirect(url_for("shipments"))
            except ValueError as error:
                db.session.rollback()
                flash(str(error), "error")
            except Exception:
                db.session.rollback()
                flash("No se pudo actualizar el envío.", "error")
        return render_template(
            "shipment_form.html",
            shipment=shipment,
            instruments=instruments,
            selected_instrument_id=shipment.instrument_id,
        )

    @app.post("/shipments/<int:shipment_id>/delete")
    def delete_shipment(shipment_id):
        shipment = db.get_or_404(Shipment, shipment_id)
        db.session.delete(shipment)
        db.session.commit()
        flash("Envío eliminado.", "success")
        return redirect(url_for("shipments"))

    @app.post("/shipments/<int:shipment_id>/transmit")
    def transmit_shipment(shipment_id):
        shipment = db.get_or_404(Shipment, shipment_id)
        _transmit(app, shipment)
        db.session.commit()
        flash(shipment.serial_status, "success" if shipment.transmitted_at else "error")
        return redirect(url_for("shipments"))

    return app


app = create_app()


if __name__ == "__main__":
    if os.getenv("AUTO_CREATE_SCHEMA", "true").lower() == "true":
        with app.app_context():
            db.create_all()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)