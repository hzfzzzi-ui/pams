FROM nginx:1.27-alpine

COPY ./public /usr/share/nginx/html

# Static mobile app config: short asset cache, API proxy, and removed legacy entrypoints.
RUN printf '%s\n' \
'server {' \
'  listen 80;' \
'  server_name _;' \
'  root /usr/share/nginx/html;' \
'  index index.html;' \
'  gzip on;' \
'  gzip_types text/plain text/css application/javascript application/json image/svg+xml;' \
'  location = /bank_reconcile.html { return 404; }' \
'  location = /cashflow.html { return 404; }' \
'  location = /holdings.html { return 404; }' \
'  location = /holdings_categories.html { return 404; }' \
'  location = /holdings_detail.html { return 404; }' \
'  location = /strategy.html { return 404; }' \
'  location = /transactions.html { return 404; }' \
'  location = /watchlist.html { return 404; }' \
'  location /holdings/ { return 302 /investments.html#holdings-smart-table; }' \
'  location ~ ^/(summary2|nav|risk|cashflow|policy|watchlist)$ { return 302 /more.html; }' \
'  location /api/ {' \
'    proxy_pass http://pams-api:8000;' \
'    proxy_set_header Host $host;' \
'    proxy_set_header X-Real-IP $remote_addr;' \
'    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;' \
'    proxy_set_header X-Forwarded-Proto $scheme;' \
'  }' \
'  location /assets/ {' \
'    add_header Cache-Control "public, max-age=300, stale-while-revalidate=1800";' \
'    try_files $uri =404;' \
'  }' \
'  location / {' \
'    try_files $uri $uri/ /index.html;' \
'  }' \
'}' \
> /etc/nginx/conf.d/default.conf
