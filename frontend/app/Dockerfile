FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY . .

ARG VITE_API_GATEWAY_URL
ARG VITE_BASE_PATH=/career-compass/
ENV VITE_API_GATEWAY_URL=$VITE_API_GATEWAY_URL
ENV VITE_BASE_PATH=$VITE_BASE_PATH
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html/career-compass
EXPOSE 8080
