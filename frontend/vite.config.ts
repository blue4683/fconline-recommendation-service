import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';

// 개발 서버 프록시·CORS·포트는 통합 담당이 INT-002로 확정하기 전까지 설정하지 않는다.
export default defineConfig({
  plugins: [vue()],
  build: {
    target: 'es2022',
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.ts'],
    restoreMocks: true,
  },
});
