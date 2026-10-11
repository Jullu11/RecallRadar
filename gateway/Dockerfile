FROM node:22-bookworm-slim
WORKDIR /app/gateway
COPY gateway/package*.json ./
RUN npm ci --include=dev
COPY gateway/ ./
USER node
CMD ["npm", "start"]
