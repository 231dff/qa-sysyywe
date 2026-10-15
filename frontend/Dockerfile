# ---- 依赖层 ----
    FROM node:20-alpine AS deps
    WORKDIR /app
    COPY package.json package-lock.json ./
    RUN npm ci
    
    # ---- 构建层 ----
    FROM node:20-alpine AS builder
    WORKDIR /app
    COPY --from=deps /app/node_modules ./node_modules
    COPY . .
    
    ENV NEXT_TELEMETRY_DISABLED=1
    # NEXT_PUBLIC_* 编译时注入（空 = 用相对路径）
    ARG NEXT_PUBLIC_BACKEND_URL=""
    ENV NEXT_PUBLIC_BACKEND_URL=$NEXT_PUBLIC_BACKEND_URL
    
    RUN npm run build
    
    # ---- 运行层 ----
    FROM node:20-alpine AS runner
    WORKDIR /app
    
    ENV NODE_ENV=production
    ENV NEXT_TELEMETRY_DISABLED=1
    ENV PORT=3000
    ENV HOSTNAME=0.0.0.0
    
    RUN addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001
    
    COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
    COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
    COPY --from=builder --chown=nextjs:nodejs /app/public ./public
    
    USER nextjs
    EXPOSE 3000
    CMD ["node", "server.js"]