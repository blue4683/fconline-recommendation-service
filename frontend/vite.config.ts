import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';

// INT-021: Agent 앱의 허용 origin은 localhost:5173·127.0.0.1:5173뿐이다.
// 다른 포트로 조용히 바뀌면 실제 연결이 CORS로 막히므로 5173이 사용 중이면 실패하게 둔다. proxy는 두지 않는다.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    target: 'es2022',
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.ts'],
    restoreMocks: true,
  },
});
