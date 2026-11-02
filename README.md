# ARINC 429 Control

Aplicación local para administrar instrumentos y envíos, codificar palabras ARINC 429 de 32 bits, registrar el resultado de una transmisión serie y calcular un valor de tensión lineal.

## Requisitos

- Python 3.10 o posterior.
- MySQL 8.x accesible desde el equipo.
- Para transmitir: adaptador/controlador serie configurado que reciba una palabra de 32 bits y la convierta a la capa física ARINC 429.
- Para una tensión física: DAC y etapa de salida adecuados para la carga/servo. El sistema no usa la salida de auriculares como fuente DC.

## Preparación

En PowerShell, desde la carpeta del proyecto:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Crear la base y un usuario en MySQL (ajustar credenciales a la instalación local):

```sql
CREATE DATABASE arinc429 CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'arinc'@'localhost' IDENTIFIED BY 'change-me';
GRANT ALL PRIVILEGES ON arinc429.* TO 'arinc'@'localhost';
```

En la primera ejecución, usar el inicializador MySQL. Crea una instancia privada en `127.0.0.1:3307`, sin modificar el servicio MySQL ya instalado, genera credenciales aleatorias, crea la base `arinc429` y un usuario limitado a ella, guarda la configuración en `.env` (ignorado por Git), crea las tablas y arranca la aplicación:

```powershell
python setup_mysql.py
```

Para usar una instalación MySQL existente, copiar `.env.example` a `.env`, completar `DATABASE_URL` y `SECRET_KEY`, y ejecutar `python app.py`. Abrir http://127.0.0.1:5000. Registrar primero un instrumento en **Instrumentos** y después usar **Crear envío**. Un envío nuevo valida el rango, genera y guarda la palabra, intenta transmitirla si el puerto está configurado, y deja el resultado visible en la tabla. Editar un envío no lo retransmite; usar **Transmitir** para reenviar explícitamente.

## Codificación y transporte

- Label: campo octal `000` a `377`, con inversión de bits para la numeración ARINC.
- SDI: bits 9-10; datos: bits 11-29; SSM: bits 30-31; paridad impar: bit 32.
- BNR: campo firmado de 19 bits en complemento a dos, escalado por la resolución configurada. BCD: cuatro dígitos decimales sin signo en nibbles; el campo BCD solo acepta enteros 0-9999.
- El entero se muestra como bits del 32 al 1. En **Hardware**, `SERIAL_BYTE_ORDER` controla aparte el orden de los cuatro bytes (`little` o `big`); debe configurarse antes de habilitar el puerto y coincidir con el adaptador.
- **Hardware** detecta puertos disponibles y permite indicar manualmente `SERIAL_PORT` (por ejemplo, `COM3`) y `SERIAL_BAUDRATE`. Los cambios quedan guardados en `.env`. No hay detección automática del protocolo del microcontrolador.

El puerto UART envía bytes con encuadre serie, no genera por sí mismo la señal eléctrica ARINC 429. Hace falta un transceptor/puente que conozca este protocolo, convierta los bytes al orden de transmisión esperado y produzca la señal diferencial bipolar ARINC. El valor transmitido debe comprobarse con el adaptador y un analizador ARINC compatibles antes de conectarlo a equipos aeronáuticos. La asignación BCD implementada es un perfil de cuatro dígitos; verificar el orden decimal requerido por el equipo receptor.

## Tensión y auriculares

Si se configuran las tensiones mínima y máxima en **Hardware**, se calcula la interpolación lineal correspondiente al rango del instrumento. Ese valor queda guardado y se muestra como **calculado**, nunca como tensión física entregada. Una salida de auriculares suele estar acoplada en AC y no puede proporcionar una continua estable; conectar un servo directamente tampoco es apropiado. Para una salida real se necesita un DAC/salida DC y una etapa de potencia protegida y dimensionada para el servo. La interfaz física queda pendiente de definir e implementar para el hardware concreto.

## Esquema de datos

- `instrumentos`: nombre, descripción, rango, label, SDI, SSM, codificación y resolución.
- `envios`: instrumento relacionado, valor, palabra de 32 bits, fecha, estado de transmisión y tensión calculada/estado.
- `envios.instrument_id` tiene clave foránea `ON DELETE RESTRICT`; el backend también impide cambiar rangos que invaliden envíos guardados.

Para pruebas unitarias/integración con SQLite en memoria:

```powershell
python -m unittest discover -s tests -v
```