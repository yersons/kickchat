# Kick Chat (web + app instalable)

Cada persona entra con su propia cuenta de Kick: los tokens se guardan por usuario en el servidor (`sessions.json`) y nunca llegan al navegador.

## 1. Registrar la app en Kick
En https://kick.com/settings/developer crea una app y registra como Redirect URI:
`https://chat.yerson.site/callback`
Copia el Client ID y el Client Secret.

## 2. Instalar en tu servidor (Ubuntu/Debian)
```bash
sudo apt update && sudo apt install -y python3-venv caddy
sudo useradd -r -m kickchat
sudo mkdir -p /opt/kickchat && sudo cp -r ./* ./.env.example /opt/kickchat/
cd /opt/kickchat
sudo python3 -m venv venv && sudo venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env      # rellena las 3 variables
sudo chown -R kickchat:kickchat /opt/kickchat
```

## 3. Dejarlo siempre encendido
```bash
sudo cp kickchat.service /etc/systemd/system/
sudo systemctl enable --now kickchat
sudo systemctl status kickchat
```

## 4. HTTPS con tu dominio
Apunta el DNS de tu dominio a la IP del servidor, edita `Caddyfile` (cambia `chat.yerson.site`) y:
```bash
sudo cp Caddyfile /etc/caddy/Caddyfile && sudo systemctl restart caddy
```
Abre `https://chat.yerson.site`. Prueba `https://chat.yerson.site/health`.

## 5. App descargable (APK)
1. Entra a https://www.pwabuilder.com, pon tu dirección y pulsa Start.
2. Package for stores → Android → genera el paquete.
3. Sube el `.apk` a tu servidor (por ejemplo a `static/`) y enlázalo para descargar. Quien lo instale debe permitir "instalar apps de origen desconocido".
4. Para Google Play usa el `.aab` y pon tu `assetlinks.json` en `static/assetlinks.json` (el servidor lo publica en `/.well-known/assetlinks.json`). La política de privacidad ya está en `/privacidad`.

Sin APK también funciona: en Chrome Android, menú → "Instalar app" (o el botón "Instalar app" del menú ⋮ de la propia página).

## Notas
- Si no salen los emotes de 7TV, mira la consola del servidor: imprime `emotes <canal>: kick=… 7tv_global=… 7tv_canal=…`.
- Se usan direcciones no oficiales de Kick (`web.kick.com`, `kick.com/api/v2`); pueden cambiar o bloquear tu IP si hay mucho tráfico.
- Para desarrollar en tu PC: `pip install -r requirements.txt`, crea `.env` con `KICK_REDIRECT_URI=http://localhost:8765/callback` y ejecuta `python server.py`.
- Copia de seguridad: `sessions.json` y `history.json` (no los subas a ningún repositorio).

## Insignias personalizadas (solo se ven en esta app)
1. Pon tu usuario de Kick en `ADMIN_USERS` del `.env` y reinicia (`sudo systemctl restart kickchat`).
2. Entra a la app, inicia sesión con Kick y abre el menú ⋮ → "Insignias (dueño)".
3. Crea una insignia (nombre + texto/emoji + color, o sube una imagen PNG/JPG/GIF/WEBP de hasta 150 KB) y asígnala escribiendo el usuario de Kick de quien la pagó.
4. Aparece junto a su nombre en todos los chats de la app, para todos los usuarios, y puedes quitarla o borrarla cuando quieras.
Se guardan en `custom_badges.json` y la carpeta `cbadges/` (respáldalos).

## Alternativa: Nginx en vez de Caddy
```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp nginx-chat.yerson.site.conf /etc/nginx/sites-available/chat.yerson.site
sudo ln -s /etc/nginx/sites-available/chat.yerson.site /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d chat.yerson.site
```
(Si usas Nginx no instales Caddy: ambos querrían los puertos 80 y 443.)
