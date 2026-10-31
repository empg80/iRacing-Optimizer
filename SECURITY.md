# Seguridad

## Qué hace y qué no hace iRacing Optimizer

| | |
|---|---|
| **Red** | No se conecta a internet. No envía datos ni telemetría a ningún sitio. |
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

## PresentMon

- Se descarga de la [release oficial de Intel](https://github.com/GameTechDev/PresentMon) durante la compilación en GitHub Actions. La compilación falla si su firma digital no es válida o no es de Intel Corporation.
- Solo captura los procesos de iRacing (`--process_name iRacingSim64DX11.exe`) y escribe un CSV junto a la sesión.
- Licencia MIT: ver [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

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
- solo se aceptan nombres `session_*.csv/.json/.presentmon.csv`, sin carpetas ni rutas;
- se ignora cualquier otro archivo del `.zip`;
- hay límites de tamaño (200 MB por CSV, 2 MB por JSON);
- nada se ejecuta ni se aplica a tu iRacing: solo se analiza.

## Verificar la descarga

Cada release incluye `SHA256SUMS.txt`. En PowerShell:

```powershell
Get-FileHash .\iRacingOptimizer.exe -Algorithm SHA256
```

El valor tiene que coincidir con el de `SHA256SUMS.txt`. El ejecutable no está firmado digitalmente, así que Windows SmartScreen puede mostrar un aviso. El `.exe` se compila en GitHub Actions a partir del código de este repositorio, con dependencias de versión fija y acciones fijadas por commit. En cada compilación se pasan los tests, el análisis estático (bandit) y la auditoría de dependencias (pip-audit).

## Avisar de un problema

Si encuentras un fallo de seguridad, abre un *issue* sin detalles explotables y pide un canal privado, o usa *Security → Report a vulnerability* en GitHub.
