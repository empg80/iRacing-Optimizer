# Seguridad

## Qué hace y qué no hace iRacing Optimizer

| | |
|---|---|
| **Red** | No se conecta a internet salvo el aviso de nuevas versiones, que es opcional y está desactivado por defecto (ver abajo). No envía datos ni telemetría a ningún sitio. |
| **Permisos** | No necesita ni pide permisos de administrador. |
| **Lee** | Los `.ini` de `Documentos\iRacing`, la telemetría en vivo de iRacing (memoria compartida, solo lectura) y datos del sistema (CPU, RAM, GPU vía driver NVIDIA, procesos en ejecución). |
| **Escribe** | Solo en `Documentos\iRacing` (los `.ini` de iRacing que están en una lista cerrada, y sus copias de seguridad en `iroptimizer_backups`) y en `Documentos\iRacingOptimizer` (sesiones, informes y perfiles). |
| **Ejecuta** | `powercfg.exe`, `nvidia-smi.exe` y `whoami.exe` desde su ruta del sistema (nunca desde la carpeta actual), y **PresentMon** (de Intel, incluido en el `.exe`) solo si activas la medición de frametimes. |
| **Permiso opcional** | PresentMon necesita que tu usuario esté en el grupo de Windows *Performance Log Users* («Usuarios del registro de rendimiento»). Si no lo está, la app lo ofrece con el botón *Dar permiso…*: Windows pide confirmación (UAC) para ejecutar un único comando de PowerShell, `Add-LocalGroupMember -SID S-1-5-32-559 -Member <tu usuario>`. La app nunca se ejecuta como administrador. |

## Protecciones

- **Antes de modificar cualquier `.ini` se hace una copia de seguridad.** La escritura es atómica: si algo falla a mitad, el archivo original queda intacto.
- **Solo se modifican claves que ya existen** en tus archivos. No se crean claves ni secciones nuevas.
- **Lista blanca de archivos**: solo `rendererDX11*.ini`, `app.ini` y `core.ini` de la carpeta de iRacing. Un perfil o un JSON no puede apuntar a otro archivo ni salir de la carpeta.
- **Valores validados**: lo que se escribe en un `.ini` solo puede contener letras, números, espacio, `.`, `_`, `+` y `-`, con un máximo de 64 caracteres. Así un perfil compartido no puede inyectar líneas, secciones ni comentarios.
- **Perfiles importados**: se limpian al importarlos y se descarta lo que no parezca un ajuste válido. La app te avisa de cuántos valores ha descartado.
- **Informes HTML**: todo el texto que procede de sesiones (nombres de circuito, coches, procesos…) se escapa, así que una sesión manipulada no puede inyectar código en el informe.
- **Restaurar copias**: solo restaura `.ini` conocidos y no admite rutas.

## Conexión a internet (aviso de versiones)

La app funciona sin internet. La única conexión que puede hacer es el aviso de nuevas versiones, **desactivado por defecto** y solo si lo aceptas:
- es una petición GET anónima a `api.github.com/repos/empg80/iRacing-Optimizer/releases/latest`, al abrir la app;
- no envía datos de tu equipo, tu configuración ni tus sesiones;
- nunca descarga ni ejecuta nada: solo abre en tu navegador enlaces del repositorio oficial en github.com (cualquier otro enlace se descarta);
- `IROPT_NO_UPDATE_CHECK=1` la desactiva del todo, también la pregunta inicial.

## PresentMon

- Se descarga de la [release oficial de Intel](https://github.com/GameTechDev/PresentMon) durante la compilación en GitHub Actions. La compilación falla si su firma digital no es válida o no es de Intel Corporation.
- Solo captura los procesos de iRacing (`--process_name iRacingSim64DX11.exe`) y escribe un CSV junto a la sesión.
- Licencia MIT: ver [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

- Para medir sin ser administrador, la app ofrece añadir tu usuario al grupo de Windows «Usuarios del registro de rendimiento» (Performance Log Users), con confirmación UAC. Ese permiso permite leer registros de rendimiento del sistema y **se queda puesto** aunque dejes de usar la app. Para quitarlo: botón «Quitar permiso…» en la pestaña Monitor, o Administración de equipos → Usuarios y grupos locales → Grupos.

## Referencia de lo aplicado

Para avisarte si iRacing cambia tu configuración, la app guarda los valores que aplicó en `Documentos\iRacingOptimizer\referencia_aplicada.json`. Ese archivo solo se usa para comparar y está sujeto a las mismas validaciones.

## Privacidad al compartir sesiones

Los archivos `session_*.json` locales incluyen tu hardware, tu configuración de iRacing y los procesos que más CPU consumieron. La ruta de tu carpeta se guarda como `%USERPROFILE%\…`, sin tu nombre de usuario.

Para compartir usa **Exportar para compartir…** (o `export`). El `.zip` resultante:
- solo contiene la telemetría, el CSV de PresentMon si existe y un JSON reducido;
- del renderer solo guarda `[Graphics Options]`, `[MonitorSetup]` y `[Display]` (sin `deviceIdx`); de `app.ini`, unas pocas claves gráficas; nada de `core.ini`;
- sustituye la lista de procesos por un único porcentaje de CPU en segundo plano, y elimina rutas y versión del sistema operativo.

## Abrir sesiones de otros

Al abrir un `.zip` recibido:
- solo se aceptan nombres `session_*.csv/.json/.presentmon.csv(.gz)`, sin carpetas ni rutas;
- se ignora cualquier otro archivo del `.zip`;
- hay límites de tamaño (200 MB por CSV, 2 MB por JSON) y de frames leídos de PresentMon (6 millones), para que un `.gz` manipulado no agote la memoria;
- nada se ejecuta ni se aplica a tu iRacing: solo se analiza.

## Verificar la descarga

Cada release incluye `SHA256SUMS.txt`. En PowerShell:

```powershell
Get-FileHash .\iRacingOptimizer.exe -Algorithm SHA256
```

El valor tiene que coincidir con el de `SHA256SUMS.txt`.

Desde la v0.9.5 cada `.exe` lleva además una **atestación de procedencia** firmada por GitHub: prueba que el archivo salió de este repositorio, de un commit concreto y del workflow público, no de un equipo particular. Para comprobarla, con [GitHub CLI](https://cli.github.com/):

```powershell
gh attestation verify .\iRacingOptimizer.exe -R empg80/iRacing-Optimizer
``` El ejecutable no está firmado digitalmente, así que Windows SmartScreen puede mostrar un aviso. El `.exe` se compila en GitHub Actions a partir del código de este repositorio, con dependencias de versión fija y acciones fijadas por commit. En cada compilación se pasan los tests, el análisis estático (bandit) y la auditoría de dependencias (pip-audit).

## Avisar de un problema

Si encuentras un fallo de seguridad, abre un *issue* sin detalles explotables y pide un canal privado, o usa *Security → Report a vulnerability* en GitHub.
