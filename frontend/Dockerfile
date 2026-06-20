FROM nginx:1.27-alpine

COPY ./public /usr/share/nginx/html

# Simple SPA-friendly config
RUN printf '%s\n' \
'server {' \
'  listen 80;' \
'  server_name _;' \
'  absolute_redirect off;' \
'  charset utf-8;' \
'  charset_types text/html text/css application/javascript application/json;' \
'  gzip on;' \
'  gzip_min_length 1024;' \
'  gzip_types text/css application/javascript application/json image/svg+xml;' \
'  location /api/ {' \
'    proxy_pass http://pams-api:8000;' \
'    proxy_http_version 1.1;' \
'    proxy_connect_timeout 30s;' \
'    proxy_send_timeout 3600s;' \
'    proxy_read_timeout 3600s;' \
'    send_timeout 3600s;' \
'    proxy_set_header Host $host;' \
'    proxy_set_header X-Real-IP $remote_addr;' \
'    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;' \
'    proxy_set_header X-Forwarded-Proto $scheme;' \
'  }' \
'  location /holdings/ {' \
'    proxy_pass http://pams-api:8000;' \
'    proxy_http_version 1.1;' \
'    proxy_connect_timeout 30s;' \
'    proxy_send_timeout 3600s;' \
'    proxy_read_timeout 3600s;' \
'    send_timeout 3600s;' \
'    proxy_set_header Host $host;' \
'    proxy_set_header X-Real-IP $remote_addr;' \
'    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;' \
'    proxy_set_header X-Forwarded-Proto $scheme;' \
'  }' \
'  location /investment/ {' \
'    proxy_pass http://pams-api:8000;' \
'    proxy_http_version 1.1;' \
'    proxy_connect_timeout 30s;' \
'    proxy_send_timeout 3600s;' \
'    proxy_read_timeout 3600s;' \
'    send_timeout 3600s;' \
'    proxy_set_header Host $host;' \
'    proxy_set_header X-Real-IP $remote_addr;' \
'    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;' \
'    proxy_set_header X-Forwarded-Proto $scheme;' \
'  }' \
'  location ~ ^/(summary2|nav|risk|cashflow|policy|watchlist)(/|$) {' \
'    proxy_pass http://pams-api:8000;' \
'    proxy_http_version 1.1;' \
'    proxy_connect_timeout 30s;' \
'    proxy_send_timeout 3600s;' \
'    proxy_read_timeout 3600s;' \
'    send_timeout 3600s;' \
'    proxy_set_header Host $host;' \
'    proxy_set_header X-Real-IP $remote_addr;' \
'    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;' \
'    proxy_set_header X-Forwarded-Proto $scheme;' \
'  }' \
'  root /usr/share/nginx/html;' \
'  index investments.html;' \
'  location = /backtest.html { return 404; }' \
'  location = /backtest_v2.html { return 404; }' \
'  location = /bank_reconcile.html { return 404; }' \
'  location = /index.html { return 302 /investments.html; }' \
'  location = /risk.html { return 302 /investments.html#portfolio-risk-disclosure; }' \
'  location = /holdings.html { return 404; }' \
'  location = /holdings_categories.html { return 404; }' \
'  location = /holdings_detail.html { return 404; }' \
'  location = /transactions.html { return 404; }' \
'  location = /cashflow.html { return 404; }' \
'  location = /strategy.html { return 404; }' \
'  location = /watchlist.html { return 404; }' \
'  location / {' \
'    add_header Cache-Control "no-store, max-age=0" always;' \
'    try_files $uri $uri/ /investments.html;' \
'  }' \
'  location /assets/ {' \
'    add_header Cache-Control "public, max-age=300, stale-while-revalidate=1800" always;' \
'    try_files $uri =404;' \
'  }' \
'}' \
> /etc/nginx/conf.d/default.conf
