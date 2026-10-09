# Patcher

Interfaz web local para administrar proyectos, ejecutar comandos rápidos y validar, previsualizar, aplicar o deshacer archivos `.patch`.

Patcher está pensado como una herramienta local para simplificar el trabajo con múltiples proyectos sin tener que cambiar constantemente entre terminales.

## Características

- Selección y búsqueda de proyectos.
- Configuración de la carpeta raíz de proyectos desde la interfaz.
- Detección automática de proyectos dentro de la raíz.
- Soporte para proyectos que todavía no utilizan Git.
- Detección de repositorios Git dentro de carpetas contenedoras.
- Proyectos fijados para acceso rápido.
- Comandos rápidos personalizados por proyecto.
- Ejecución de comandos dentro del directorio del proyecto seleccionado.
- Consola de ejecución con estado y salida.
- Drag & drop de archivos `.patch`.
- Validación mediante `git apply --check`.
- Previsualización visual de cambios.
- Aplicación y reversión de patches.

---

## Instalar

Clona el repositorio:

```bash
git clone https://github.com/serome111/Patcher.git
cd patcher
```

Crea el entorno virtual:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Instala las dependencias:

```bash
pip install -r requirements.txt
```

## Ejecutar

Desde la carpeta de Patcher:

```bash
uvicorn app:app --host 127.0.0.1 --port 8040 --reload
```

Abre en el navegador:

```text
http://127.0.0.1:8044
```

Patcher funciona completamente de forma local.

---

# Configurar proyectos

Patcher trabaja sobre una **carpeta raíz de proyectos**.

Por ejemplo:

```text
Projects/
├── frontend/
├── backend/
├── experiments/
├── game/
└── company-projects/
```

La raíz puede configurarse directamente desde la interfaz.

En la parte superior de Patcher podrás ver la carpeta actualmente utilizada y cambiarla cuando sea necesario.

La selección queda guardada en:

```text
patcher_config.json
```

por lo que no necesitas volver a configurarla cada vez que inicias Patcher.

## Configurar la raíz mediante variable de entorno

También puedes establecer la raíz al iniciar la aplicación:

```bash
PATCHER_ROOT=/ruta/a/mis/proyectos \
uvicorn app:app --host 127.0.0.1 --port 8040
```

Cuando `PATCHER_ROOT` está definido, tiene prioridad sobre la configuración de la interfaz.

La prioridad es:

```text
PATCHER_ROOT
      ↓
Raíz guardada en patcher_config.json
      ↓
Directorio actual
```

Esto permite utilizar Patcher en diferentes entornos sin modificar el código.

---

# Detección de proyectos

Todas las carpetas directamente dentro de la raíz pueden utilizarse como proyectos.

No es necesario que sean repositorios Git.

Por ejemplo:

```text
Projects/
├── proyecto-nuevo/
│
├── frontend/
│   └── .git/
│
└── plataforma/
    ├── api/
    │   └── .git/
    │
    └── web/
        └── .git/
```

Patcher detectará:

```text
proyecto-nuevo
frontend
plataforma / api
plataforma / web
```

Esto permite utilizar carpetas únicamente como organización.

Por ejemplo:

```text
Projects/
└── company/
    ├── backend/
    │   └── .git/
    └── frontend/
        └── .git/
```

`company` no necesita ser un monorepo.

Patcher detectará los repositorios internos de forma independiente.

---

# Proyectos sin Git

Un proyecto no necesita tener `.git` para aparecer en Patcher.

Esto permite trabajar con proyectos nuevos antes de inicializar Git.

Los proyectos sin Git pueden utilizar funciones como:

- selección de proyecto;
- proyectos fijados;
- comandos rápidos.

Las operaciones relacionadas con archivos `.patch` requieren un repositorio Git.

---

# Selector de proyectos

El buscador principal permite localizar rápidamente proyectos dentro de la raíz configurada.

Los proyectos utilizados frecuentemente pueden fijarse para que aparezcan como accesos rápidos debajo del buscador.

Esto evita recorrer manualmente una lista grande de proyectos.

---

# Comandos rápidos

Cada proyecto puede tener sus propios comandos rápidos.

Por ejemplo:

```bash
npm run dev
```

```bash
npm run build
```

```bash
docker compose down
docker compose up -d --build
```

```bash
pytest
```

Los comandos se ejecutan utilizando como directorio de trabajo el proyecto seleccionado.

Por ejemplo:

```text
Projects/
└── backend/
```

Si `backend` está seleccionado, el comando se ejecuta como si hubieras hecho:

```bash
cd Projects/backend
```

antes de ejecutarlo.

## Salida de comandos

Mientras un comando está ejecutándose, Patcher muestra su salida.

Al finalizar, la consola se minimiza automáticamente.

Un comando exitoso muestra:

```text
✓ Listo · Ver detalle
```

Un comando que falla muestra:

```text
✕ Error · Ver detalle
```

Puedes utilizar **Ver detalle** para volver a abrir la salida completa.

---

# Aplicar patches

Puedes arrastrar un archivo `.patch` directamente sobre la interfaz.

Antes de permitir aplicarlo, Patcher ejecuta:

```bash
git apply --check archivo.patch
```

Esto permite comprobar que el patch puede aplicarse sobre el proyecto seleccionado.

Si la validación es correcta, Patcher muestra una previsualización de los cambios.

La previsualización incluye:

- archivos afectados;
- líneas añadidas;
- líneas eliminadas;
- bloques modificados.

Después puedes aplicar el patch mediante:

```bash
git apply archivo.patch
```

---

# Revertir un patch

Después de aplicar un patch, Patcher permite revertirlo mediante:

```bash
git apply --reverse archivo.patch
```

Patcher no utiliza:

```bash
git reset --hard
```

y tampoco crea commits automáticamente.

El estado del repositorio continúa bajo control del desarrollador.

---

# Configuración

Patcher guarda su configuración local en:

```text
patcher_config.json
```

La configuración puede contener información como:

```json
{
  "projects_root": "/ruta/a/mis/proyectos",
  "pinned": [],
  "commands": []
}
```

Aquí se almacenan:

- carpeta raíz seleccionada;
- proyectos fijados;
- comandos rápidos.

`patcher_config.json` representa configuración local de la instalación y normalmente no debería compartirse entre diferentes máquinas.

---

# Seguridad

Patcher está diseñado para ejecutarse como herramienta de desarrollo local.

Las operaciones de archivos están limitadas a la raíz de proyectos configurada.

Los patches son validados para evitar rutas inseguras como:

```text
../
```

o rutas absolutas fuera del proyecto.

Los comandos rápidos pueden ejecutar comandos del sistema.

Por esta razón, deben configurarse únicamente comandos de confianza.

No se recomienda exponer Patcher directamente a Internet.

---

# Flujo de trabajo

El flujo principal de Patcher es:

```text
Configurar raíz
       ↓
Seleccionar proyecto
       ↓
Cargar patch
       ↓
Validar
       ↓
Revisar cambios
       ↓
Aplicar
       ↓
Ejecutar / probar
       ↓
Revertir si es necesario
```

También puede utilizarse simplemente como launcher local de comandos:

```text
Seleccionar proyecto
       ↓
Ejecutar comando rápido
       ↓
Ver resultado
```

El objetivo es reducir operaciones repetitivas sin ocultar las herramientas que Patcher utiliza por debajo.