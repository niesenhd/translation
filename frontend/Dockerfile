FROM node:20.17.0-alpine AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --registry=https://registry.npmmirror.com

FROM dependencies AS test
COPY . .
CMD ["npm", "test"]

FROM dependencies AS build
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27.3-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
