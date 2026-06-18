FROM nginx:1.27.2-alpine

COPY nginx.conf /etc/nginx/nginx.conf

COPY public/ /usr/share/nginx/html/

RUN chown -R nginx:nginx /var/cache/nginx /var/log/nginx /usr/share/nginx/html \
 && touch /var/run/nginx.pid && chown nginx:nginx /var/run/nginx.pid

USER nginx

EXPOSE 8000 8001

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O - http://127.0.0.1:8000/ >/dev/null || exit 1
