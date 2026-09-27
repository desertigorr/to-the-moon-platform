# Frontend To the Moon

React/TypeScript диспетчерский интерфейс. Полный запуск решения и инструкции жюри — в [корневом README](../README.md).

Локальная разработка: Node 24, pnpm 11.25.0, `pnpm install --frozen-lockfile`, `pnpm dev --port 5174`. Прокси направляет `/api` на localhost:8000.

Проверки: `pnpm check`, `pnpm test`, `pnpm build`. Тесты не требуют работающего backend. Основной экран — `src/features/telemetry/DispatcherDashboard.tsx`; типизированный контракт — `contract.ts`.
