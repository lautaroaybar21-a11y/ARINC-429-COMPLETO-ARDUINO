import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

import pymysql
from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"
PORT = 3307
DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "ARINC429" / "mysql-data"
LOG_FILE = DATA_DIR.parent / "mysql.log"


def _find_mysql():
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    for version in ("8.4", "8.0"):
        basedir = program_files / "MySQL" / f"MySQL Server {version}"
        mysqld = basedir / "bin" / "mysqld.exe"
        if mysqld.is_file():
            return basedir, mysqld
    raise RuntimeError("No se encontró MySQL Server 8.4/8.0 en Program Files.")


def _initialize_mysql(basedir, mysqld):
    if DATA_DIR.exists():
        raise RuntimeError(
            f"Existe {DATA_DIR} pero no existe .env. No voy a sobrescribir esos datos; "
            "revisá la configuración local antes de continuar."
        )
    with socket.socket() as port_check:
        if port_check.connect_ex(("127.0.0.1", PORT)) == 0:
            raise RuntimeError(f"El puerto local {PORT} ya está ocupado.")

    DATA_DIR.mkdir(parents=True)
    init = subprocess.run(
        [
            str(mysqld),
            "--no-defaults",
            "--initialize-insecure",
            f"--basedir={basedir}",
            f"--datadir={DATA_DIR}",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if init.returncode:
        raise RuntimeError(f"No se pudo inicializar MySQL: {init.stderr[-2000:]}")

    log = LOG_FILE.open("a", encoding="utf-8")
    subprocess.Popen(
        [
            str(mysqld),
            "--no-defaults",
            f"--basedir={basedir}",
            f"--datadir={DATA_DIR}",
            f"--port={PORT}",
            "--bind-address=127.0.0.1",
            "--mysqlx=0",
            f"--log-error={LOG_FILE}",
        ],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    log.close()

    for _ in range(60):
        try:
            return pymysql.connect(
                host="127.0.0.1",
                port=PORT,
                user="root",
                password="",
                connect_timeout=1,
                autocommit=True,
            )
        except pymysql.MySQLError:
            time.sleep(1)
    raise RuntimeError(f"MySQL no inició. Revisá el log local: {LOG_FILE}")


def configure_mysql():
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)
        return

    print("Preparando una instancia MySQL local privada en 127.0.0.1:3307.")
    print("No se modifica ni se necesita la contraseña del MySQL ya instalado.")
    basedir, mysqld = _find_mysql()
    connection = _initialize_mysql(basedir, mysqld)
    app_password = secrets.token_urlsafe(36)
    root_password = secrets.token_urlsafe(48)

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE IF NOT EXISTS arinc429 "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            for host in ("127.0.0.1", "localhost"):
                cursor.execute(
                    f"CREATE USER IF NOT EXISTS 'arinc_app'@'{host}' IDENTIFIED BY %s",
                    (app_password,),
                )
                cursor.execute(
                    f"ALTER USER 'arinc_app'@'{host}' IDENTIFIED BY %s",
                    (app_password,),
                )
                cursor.execute(
                    f"GRANT ALL PRIVILEGES ON arinc429.* TO 'arinc_app'@'{host}'"
                )
            cursor.execute(
                "ALTER USER 'root'@'localhost' IDENTIFIED BY %s",
                (root_password,),
            )
        connection.close()
    except pymysql.MySQLError as error:
        connection.close()
        print(f"No se pudo configurar la instancia local: {error}")
        raise SystemExit(1) from None

    database_url = (
        f"mysql+pymysql://arinc_app:{app_password}"
        f"@127.0.0.1:{PORT}/arinc429?charset=utf8mb4"
    )
    env_values = "\n".join(
        [
            f"SECRET_KEY={secrets.token_urlsafe(32)}",
            f"DATABASE_URL={database_url}",
            "SERIAL_PORT=",
            "SERIAL_BAUDRATE=115200",
            "SERIAL_TIMEOUT=2",
            "SERIAL_BYTE_ORDER=",
            "OUTPUT_VOLTAGE_MIN=",
            "OUTPUT_VOLTAGE_MAX=",
            "AUTO_CREATE_SCHEMA=true",
            "PORT=5000",
            "",
        ]
    )
    try:
        ENV_FILE.write_text(env_values, encoding="utf-8")
    except OSError as error:
        print(f"MySQL quedó configurado, pero no se pudo escribir .env: {error}")
        print("No compartas ni publiques la contraseña del usuario arinc_app.")
        raise SystemExit(1) from None

    load_dotenv(ENV_FILE, override=True)
    print("MySQL privado configurado; los datos quedan fuera del repositorio.")


def main():
    configure_mysql()
    from app import create_app, db

    application = create_app()
    with application.app_context():
        db.create_all()
    print("Aplicación conectada a MySQL en http://127.0.0.1:5000")
    application.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)


if __name__ == "__main__":
    main()