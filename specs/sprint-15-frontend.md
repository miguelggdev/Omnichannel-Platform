# Sprint 15 — Frontend Foundation & Panel de Administración (Fase 4)

> **Hito Frontend** — Al completar este sprint, la plataforma SaaS tiene una interfaz de usuario completa y funcional que consume todos los endpoints de la API backend (Sprints 1-14).

## Objetivo

Implementar la aplicación frontend completa para la plataforma SaaS multi-tenant de IA conversacional, incluyendo panel de administración, onboarding de clientes, personalización de negocios, gestión de conversaciones en tiempo real, dashboard de analítica, y todas las interfaces de gestión. La aplicación soporta 6 idiomas, tema claro/oscuro, diseño responsive y control de acceso basado en roles (RBAC).

## Prerequisitos

- Sprints 1–14 completados: todos los endpoints de la API backend funcionales bajo `/api/v1/`.
- Autenticación JWT (access + refresh tokens) operativa en el backend.
- RBAC con 4 roles: `super_admin`, `admin`, `supervisor`, `agent`.
- Supabase Realtime accesible para suscripción a mensajes en vivo.
- Traefik configurado como reverse proxy en Docker Compose.
- Node.js 20+ LTS disponible para el build.

## Stack Frontend

| Categoría | Tecnología | Versión |
|---|---|---|
| Framework | Next.js (App Router) | 14.2+ |
| Lenguaje | TypeScript | 5.4+ |
| UI Components | shadcn/ui (Radix UI) | latest |
| Estilos | Tailwind CSS | 3.4+ |
| Estado global | Zustand | 4.5+ |
| Server state | TanStack Query (React Query) | 5.x |
| Formularios | React Hook Form + Zod | 7.x + 3.x |
| Internacionalización | next-intl | 3.x |
| Gráficos | Recharts | 2.12+ |
| Tiempo real | @supabase/supabase-js (Realtime) | 2.x |
| Íconos | Lucide React | latest |
| Tema | next-themes | 0.3+ |
| HTTP Client | Axios | 1.7+ |
| Fechas | date-fns | 3.x |
| Upload | react-dropzone | 14.x |

## Archivos a Crear

```
frontend/
├── package.json
├── next.config.js
├── tailwind.config.ts
├── tsconfig.json
├── postcss.config.js
├── .env.example
├── .env.local                        # Ignorado por git
├── Dockerfile                        # Multi-stage build
├── .dockerignore
├── public/
│   ├── favicon.ico
│   └── locales/
│       ├── es/common.json
│       ├── en/common.json
│       ├── pt/common.json
│       ├── it/common.json
│       ├── de/common.json
│       └── fr/common.json
├── src/
│   ├── app/
│   │   ├── layout.tsx                # Root layout con providers
│   │   ├── not-found.tsx             # Página 404
│   │   ├── error.tsx                 # Error boundary global
│   │   ├── loading.tsx               # Loading global
│   │   ├── (auth)/
│   │   │   ├── layout.tsx            # Layout centrado sin sidebar
│   │   │   ├── login/page.tsx
│   │   │   └── onboarding/page.tsx   # Auto-registro de clientes
│   │   ├── (dashboard)/
│   │   │   ├── layout.tsx            # Layout con sidebar + header
│   │   │   ├── page.tsx              # Dashboard home
│   │   │   ├── conversations/
│   │   │   │   ├── page.tsx          # Lista de conversaciones
│   │   │   │   └── [id]/page.tsx     # Chat view en detalle
│   │   │   ├── contacts/
│   │   │   │   ├── page.tsx          # Lista de contactos
│   │   │   │   └── [id]/page.tsx     # Detalle de contacto
│   │   │   ├── documents/
│   │   │   │   └── page.tsx          # Gestión de knowledge base
│   │   │   ├── settings/
│   │   │   │   ├── page.tsx          # Redirect a business-profile
│   │   │   │   ├── business-profile/page.tsx
│   │   │   │   ├── agents/page.tsx   # Config de agentes IA
│   │   │   │   ├── channels/page.tsx # Canales conectados
│   │   │   │   ├── quick-replies/page.tsx
│   │   │   │   ├── team/page.tsx     # Gestión de usuarios
│   │   │   │   └── preferences/page.tsx  # Tema, idioma, notificaciones
│   │   │   └── analytics/
│   │   │       └── page.tsx          # Dashboard de analítica
│   │   └── (super-admin)/
│   │       ├── layout.tsx            # Layout con guard super_admin
│   │       ├── clients/
│   │       │   ├── page.tsx          # Lista de clientes/tenants
│   │       │   └── [id]/page.tsx     # Detalle de cliente
│   │       ├── celery/page.tsx       # Admin de Celery/Redis
│   │       ├── system/page.tsx       # Salud del sistema
│   │       └── security/page.tsx     # Visión general de seguridad
│   ├── components/
│   │   ├── ui/                       # Componentes shadcn/ui generados
│   │   │   ├── button.tsx
│   │   │   ├── input.tsx
│   │   │   ├── label.tsx
│   │   │   ├── card.tsx
│   │   │   ├── dialog.tsx
│   │   │   ├── dropdown-menu.tsx
│   │   │   ├── select.tsx
│   │   │   ├── table.tsx
│   │   │   ├── tabs.tsx
│   │   │   ├── badge.tsx
│   │   │   ├── toast.tsx
│   │   │   ├── tooltip.tsx
│   │   │   ├── avatar.tsx
│   │   │   ├── separator.tsx
│   │   │   ├── sheet.tsx             # Para sidebar móvil
│   │   │   ├── skeleton.tsx
│   │   │   ├── switch.tsx
│   │   │   ├── textarea.tsx
│   │   │   ├── progress.tsx
│   │   │   ├── scroll-area.tsx
│   │   │   ├── command.tsx           # Para búsqueda rápida (Cmd+K)
│   │   │   └── sonner.tsx            # Toasts con sonner
│   │   ├── layout/
│   │   │   ├── Sidebar.tsx           # Navegación principal
│   │   │   ├── SidebarItem.tsx       # Item individual con ícono + badge
│   │   │   ├── Header.tsx            # Barra superior
│   │   │   ├── ThemeToggle.tsx       # Switch Light/Dark/System
│   │   │   ├── LanguageSelector.tsx  # Dropdown de idioma con banderas
│   │   │   ├── UserMenu.tsx          # Avatar + dropdown usuario
│   │   │   └── Breadcrumbs.tsx       # Navegación de contexto
│   │   ├── auth/
│   │   │   ├── LoginForm.tsx
│   │   │   ├── OnboardingWizard.tsx  # Formulario multi-paso
│   │   │   └── ProtectedRoute.tsx    # Guard de autenticación + RBAC
│   │   ├── conversations/
│   │   │   ├── ConversationList.tsx   # Lista con búsqueda y filtros
│   │   │   ├── ConversationItem.tsx   # Item individual en la lista
│   │   │   ├── ChatView.tsx          # Vista de chat completa
│   │   │   ├── MessageBubble.tsx     # Burbuja de mensaje
│   │   │   ├── MessageInput.tsx      # Input con quick replies
│   │   │   ├── ConversationHeader.tsx # Info + acciones de conversación
│   │   │   └── ContactSidebar.tsx    # Panel lateral con info del contacto
│   │   ├── contacts/
│   │   │   ├── ContactList.tsx
│   │   │   ├── ContactDetail.tsx
│   │   │   └── ContactForm.tsx
│   │   ├── documents/
│   │   │   ├── DocumentUpload.tsx    # Drag-and-drop con preview
│   │   │   ├── DocumentList.tsx
│   │   │   └── DocumentStatus.tsx    # Badge de estado de procesamiento
│   │   ├── dashboard/
│   │   │   ├── StatCard.tsx          # Tarjeta de métrica individual
│   │   │   ├── ConversationsByChannel.tsx  # Gráfico de pie
│   │   │   ├── MessagesOverTime.tsx  # Gráfico de líneas
│   │   │   └── RecentConversations.tsx
│   │   ├── settings/
│   │   │   ├── BusinessProfileForm.tsx
│   │   │   ├── OperatingHoursEditor.tsx
│   │   │   ├── AgentConfigForm.tsx
│   │   │   ├── QuickReplyManager.tsx
│   │   │   ├── TeamManager.tsx
│   │   │   └── ChannelManager.tsx
│   │   ├── super-admin/
│   │   │   ├── ClientList.tsx
│   │   │   ├── ClientDetail.tsx
│   │   │   ├── CeleryDashboard.tsx
│   │   │   ├── WorkerStatusTable.tsx
│   │   │   ├── QueueDepthChart.tsx
│   │   │   ├── TaskHistoryTable.tsx
│   │   │   └── SystemHealthPanel.tsx
│   │   └── charts/
│   │       ├── PieChart.tsx          # Wrapper de Recharts
│   │       ├── LineChart.tsx
│   │       ├── BarChart.tsx
│   │       └── GaugeChart.tsx        # Para memoria Redis
│   ├── lib/
│   │   ├── api.ts                    # Cliente API (axios) con interceptores
│   │   ├── auth.ts                   # Utilidades de autenticación
│   │   ├── supabase.ts              # Cliente Supabase Realtime
│   │   ├── utils.ts                  # Utilidades generales (cn, formatDate, etc.)
│   │   └── constants.ts             # Constantes de la app
│   ├── hooks/
│   │   ├── useAuth.ts               # Login, logout, refresh, user actual
│   │   ├── useTheme.ts              # Gestión de tema
│   │   ├── useConversations.ts      # CRUD + realtime de conversaciones
│   │   ├── useContacts.ts           # CRUD de contactos
│   │   ├── useDocuments.ts          # Upload, list, delete documentos
│   │   ├── useDashboard.ts          # Métricas del dashboard
│   │   ├── useUsers.ts             # Gestión de equipo (team)
│   │   ├── useQuickReplies.ts      # CRUD de respuestas rápidas
│   │   ├── useBusinessProfile.ts   # Perfil del negocio
│   │   └── useCelery.ts            # Métricas de Celery (super_admin)
│   ├── stores/
│   │   ├── authStore.ts             # Estado de autenticación (Zustand)
│   │   └── uiStore.ts              # Estado de UI (sidebar, modales, etc.)
│   ├── types/
│   │   ├── index.ts                 # Re-exports
│   │   ├── auth.ts                  # User, LoginRequest, TokenResponse
│   │   ├── client.ts               # Client, BusinessProfile
│   │   ├── contact.ts              # Contact, ContactIdentifier, Tag
│   │   ├── conversation.ts         # Conversation, ConversationStatus
│   │   ├── message.ts              # Message, MessageType, MessageDirection
│   │   ├── document.ts             # Document, DocumentStatus
│   │   ├── agent.ts                # AgentConfig, AgentType
│   │   ├── analytics.ts            # DashboardMetrics, ChartData
│   │   ├── celery.ts               # WorkerStatus, TaskInfo, QueueDepth
│   │   └── common.ts               # PaginatedResponse, ApiError
│   ├── i18n/
│   │   ├── config.ts               # Configuración de next-intl
│   │   └── request.ts              # getRequestConfig para server components
│   └── middleware.ts                # Next.js middleware (auth + i18n)
```

## Tareas Detalladas

### 1. Configuración Base del Proyecto

**1.1 Inicialización de Next.js con TypeScript**

```bash
npx create-next-app@latest frontend --typescript --tailwind --eslint --app --src-dir
cd frontend
```

**1.2 `next.config.js`**

```javascript
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  images: {
    remotePatterns: [
      {
        protocol: 'https',
        hostname: '*.supabase.co',
      },
    ],
  },
  async rewrites() {
    return [
      {
        source: '/api/v1/:path*',
        destination: `${process.env.NEXT_PUBLIC_API_URL}/api/v1/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
```

**1.3 `.env.example`**

```env
# API Backend
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_APP_URL=http://localhost:3000

# Supabase Realtime
NEXT_PUBLIC_SUPABASE_URL=http://localhost:8000
NEXT_PUBLIC_SUPABASE_ANON_KEY=your-supabase-anon-key

# Internacionalización
NEXT_PUBLIC_DEFAULT_LOCALE=es

# Feature Flags
NEXT_PUBLIC_ENABLE_SUPER_ADMIN=true
```

**1.4 `tailwind.config.ts`**

```typescript
import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./src/**/*.{ts,tsx}",
    "./src/components/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Colores semánticos usando CSS variables
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        // Colores de estado para conversaciones
        status: {
          new: "#3B82F6",          // blue-500
          bot_active: "#8B5CF6",   // violet-500
          human_active: "#10B981", // emerald-500
          waiting_human: "#F59E0B",// amber-500
          waiting_client: "#6366F1",// indigo-500
          resolved: "#6B7280",     // gray-500
          archived: "#9CA3AF",     // gray-400
        },
        // Colores de canal
        channel: {
          whatsapp: "#25D366",
          instagram: "#E4405F",
          facebook: "#1877F2",
          web: "#6366F1",
          voice: "#8B5CF6",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      keyframes: {
        "slide-in": {
          from: { transform: "translateX(-100%)" },
          to: { transform: "translateX(0)" },
        },
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
      },
      animation: {
        "slide-in": "slide-in 0.3s ease-out",
        "fade-in": "fade-in 0.2s ease-out",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
```

**1.5 Variables CSS para temas — `src/app/globals.css`**

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  :root {
    /* Tema claro */
    --background: 0 0% 100%;
    --foreground: 222.2 84% 4.9%;
    --card: 0 0% 100%;
    --card-foreground: 222.2 84% 4.9%;
    --primary: 221.2 83.2% 53.3%;       /* blue-600 */
    --primary-foreground: 210 40% 98%;
    --secondary: 210 40% 96.1%;
    --secondary-foreground: 222.2 47.4% 11.2%;
    --muted: 210 40% 96.1%;
    --muted-foreground: 215.4 16.3% 46.9%;
    --accent: 210 40% 96.1%;
    --accent-foreground: 222.2 47.4% 11.2%;
    --destructive: 0 84.2% 60.2%;
    --destructive-foreground: 210 40% 98%;
    --border: 214.3 31.8% 91.4%;
    --input: 214.3 31.8% 91.4%;
    --ring: 221.2 83.2% 53.3%;
    --radius: 0.5rem;
  }

  .dark {
    /* Tema oscuro */
    --background: 222.2 84% 4.9%;       /* slate-950 */
    --foreground: 210 40% 98%;           /* slate-50 */
    --card: 222.2 84% 4.9%;
    --card-foreground: 210 40% 98%;
    --primary: 217.2 91.2% 59.8%;       /* blue-400 */
    --primary-foreground: 222.2 47.4% 11.2%;
    --secondary: 217.2 32.6% 17.5%;
    --secondary-foreground: 210 40% 98%;
    --muted: 217.2 32.6% 17.5%;
    --muted-foreground: 215 20.2% 65.1%;
    --accent: 217.2 32.6% 17.5%;
    --accent-foreground: 210 40% 98%;
    --destructive: 0 62.8% 30.6%;
    --destructive-foreground: 210 40% 98%;
    --border: 217.2 32.6% 17.5%;
    --input: 217.2 32.6% 17.5%;
    --ring: 224.3 76.3% 48%;
  }
}

@layer base {
  * {
    @apply border-border;
  }
  body {
    @apply bg-background text-foreground;
  }
}
```

---

### 2. Sistema de Autenticación — `src/lib/api.ts`, `src/lib/auth.ts`, `src/stores/authStore.ts`

**2.1 Cliente API con interceptores — `src/lib/api.ts`**

```typescript
import axios, { AxiosError, InternalAxiosRequestConfig } from "axios";
import { useAuthStore } from "@/stores/authStore";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export const api = axios.create({
  baseURL: `${API_BASE_URL}/api/v1`,
  headers: {
    "Content-Type": "application/json",
  },
  withCredentials: true,
});

// Interceptor de request: agregar access token
api.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    const token = useAuthStore.getState().accessToken;
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// Interceptor de response: refresh automático en 401
api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & {
      _retry?: boolean;
    };

    if (error.response?.status === 401 && !originalRequest._retry) {
      originalRequest._retry = true;

      try {
        const refreshToken = useAuthStore.getState().refreshToken;
        if (!refreshToken) {
          useAuthStore.getState().logout();
          window.location.href = "/login";
          return Promise.reject(error);
        }

        const response = await axios.post(
          `${API_BASE_URL}/api/v1/auth/refresh`,
          { refresh_token: refreshToken }
        );

        const { access_token, refresh_token } = response.data;
        useAuthStore.getState().setTokens(access_token, refresh_token);

        originalRequest.headers.Authorization = `Bearer ${access_token}`;
        return api(originalRequest);
      } catch (refreshError) {
        useAuthStore.getState().logout();
        window.location.href = "/login";
        return Promise.reject(refreshError);
      }
    }

    return Promise.reject(error);
  }
);

// Helpers tipados
export async function apiGet<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  const response = await api.get<T>(url, { params });
  return response.data;
}

export async function apiPost<T>(url: string, data?: unknown): Promise<T> {
  const response = await api.post<T>(url, data);
  return response.data;
}

export async function apiPut<T>(url: string, data?: unknown): Promise<T> {
  const response = await api.put<T>(url, data);
  return response.data;
}

export async function apiPatch<T>(url: string, data?: unknown): Promise<T> {
  const response = await api.patch<T>(url, data);
  return response.data;
}

export async function apiDelete<T>(url: string): Promise<T> {
  const response = await api.delete<T>(url);
  return response.data;
}
```

**2.2 Store de autenticación — `src/stores/authStore.ts`**

```typescript
import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { User, UserRole } from "@/types/auth";

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;

  // Acciones
  setTokens: (accessToken: string, refreshToken: string) => void;
  setUser: (user: User) => void;
  logout: () => void;
  setLoading: (loading: boolean) => void;

  // Helpers RBAC
  hasRole: (role: UserRole) => boolean;
  hasMinRole: (minRole: UserRole) => boolean;
}

const ROLE_HIERARCHY: Record<UserRole, number> = {
  super_admin: 4,
  admin: 3,
  supervisor: 2,
  agent: 1,
};

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      accessToken: null,
      refreshToken: null,
      user: null,
      isAuthenticated: false,
      isLoading: true,

      setTokens: (accessToken, refreshToken) =>
        set({ accessToken, refreshToken, isAuthenticated: true }),

      setUser: (user) => set({ user }),

      logout: () =>
        set({
          accessToken: null,
          refreshToken: null,
          user: null,
          isAuthenticated: false,
        }),

      setLoading: (isLoading) => set({ isLoading }),

      hasRole: (role) => get().user?.role === role,

      hasMinRole: (minRole) => {
        const userRole = get().user?.role;
        if (!userRole) return false;
        return ROLE_HIERARCHY[userRole] >= ROLE_HIERARCHY[minRole];
      },
    }),
    {
      name: "auth-storage",
      partialize: (state) => ({
        accessToken: state.accessToken,
        refreshToken: state.refreshToken,
        user: state.user,
        isAuthenticated: state.isAuthenticated,
      }),
    }
  )
);
```

**2.3 Store de UI — `src/stores/uiStore.ts`**

```typescript
import { create } from "zustand";

interface UIState {
  sidebarOpen: boolean;
  sidebarCollapsed: boolean;
  commandPaletteOpen: boolean;

  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleCommandPalette: () => void;
}

export const useUIStore = create<UIState>()((set) => ({
  sidebarOpen: false,
  sidebarCollapsed: false,
  commandPaletteOpen: false,

  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),
  toggleCommandPalette: () =>
    set((s) => ({ commandPaletteOpen: !s.commandPaletteOpen })),
}));
```

**2.4 Hook de autenticación — `src/hooks/useAuth.ts`**

```typescript
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { api, apiPost, apiGet } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import type { LoginRequest, TokenResponse, User } from "@/types/auth";

export function useAuth() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { setTokens, setUser, logout: clearAuth, isAuthenticated } = useAuthStore();

  // Obtener perfil del usuario actual
  const { data: user, isLoading: isLoadingUser } = useQuery({
    queryKey: ["me"],
    queryFn: () => apiGet<User>("/auth/me"),
    enabled: isAuthenticated,
    retry: false,
    staleTime: 5 * 60 * 1000, // 5 minutos
    onSuccess: (data: User) => setUser(data),
    onError: () => clearAuth(),
  });

  // Login
  const loginMutation = useMutation({
    mutationFn: (data: LoginRequest) =>
      apiPost<TokenResponse>("/auth/login", data),
    onSuccess: (data) => {
      setTokens(data.access_token, data.refresh_token);
      queryClient.invalidateQueries({ queryKey: ["me"] });
      router.push("/");
    },
  });

  // Logout
  const logoutMutation = useMutation({
    mutationFn: () => apiPost("/auth/logout"),
    onSettled: () => {
      clearAuth();
      queryClient.clear();
      router.push("/login");
    },
  });

  return {
    user,
    isLoadingUser,
    isAuthenticated,
    login: loginMutation.mutateAsync,
    loginError: loginMutation.error,
    isLoggingIn: loginMutation.isPending,
    logout: logoutMutation.mutate,
  };
}
```

---

### 3. Dark/Light Theme Toggle

**3.1 Proveedor de tema — integrado en `src/app/layout.tsx`**

```typescript
import { ThemeProvider } from "next-themes";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"] });

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es" suppressHydrationWarning>
      <body className={inter.className}>
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          <QueryProvider>
            {children}
          </QueryProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
```

**3.2 Componente ThemeToggle — `src/components/layout/ThemeToggle.tsx`**

```typescript
"use client";

import { useTheme } from "next-themes";
import { useEffect, useState } from "react";
import { Sun, Moon, Monitor } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { useTranslations } from "next-intl";

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  const t = useTranslations("common");

  // Evitar hydration mismatch
  useEffect(() => setMounted(true), []);
  if (!mounted) return <Button variant="ghost" size="icon" className="h-9 w-9" />;

  const options = [
    { value: "light", label: t("theme.light"), icon: Sun },
    { value: "dark", label: t("theme.dark"), icon: Moon },
    { value: "system", label: t("theme.system"), icon: Monitor },
  ] as const;

  const CurrentIcon = options.find((o) => o.value === theme)?.icon ?? Monitor;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-9 w-9"
          aria-label={t("theme.toggle")}
        >
          <CurrentIcon className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {options.map(({ value, label, icon: Icon }) => (
          <DropdownMenuItem
            key={value}
            onClick={() => setTheme(value)}
            className={theme === value ? "bg-accent" : ""}
          >
            <Icon className="mr-2 h-4 w-4" />
            {label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
```

Consideraciones de accesibilidad:
- `aria-label` en el botón de toggle.
- Indicador visual del tema activo (`bg-accent`).
- `suppressHydrationWarning` en `<html>` para evitar mismatch SSR.
- `disableTransitionOnChange` para evitar flashes al cambiar tema.

---

### 4. Diseño Responsive — Layout con Sidebar

**4.1 Sidebar principal — `src/components/layout/Sidebar.tsx`**

```typescript
"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";
import {
  MessageSquare, Users, FileText, Settings, BarChart3,
  Home, Shield, Server, Activity, ChevronLeft, Menu,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/authStore";
import { useUIStore } from "@/stores/uiStore";
import { useTranslations } from "next-intl";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { SidebarItem } from "./SidebarItem";

interface NavItem {
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  labelKey: string;
  minRole?: "agent" | "supervisor" | "admin" | "super_admin";
  badge?: number;
}

export function Sidebar() {
  const t = useTranslations("nav");
  const pathname = usePathname();
  const { hasMinRole } = useAuthStore();
  const { sidebarCollapsed, setSidebarCollapsed, sidebarOpen, setSidebarOpen } =
    useUIStore();

  const navItems: NavItem[] = [
    { href: "/", icon: Home, labelKey: "dashboard" },
    { href: "/conversations", icon: MessageSquare, labelKey: "conversations" },
    { href: "/contacts", icon: Users, labelKey: "contacts" },
    { href: "/documents", icon: FileText, labelKey: "documents" },
    { href: "/analytics", icon: BarChart3, labelKey: "analytics", minRole: "supervisor" },
    { href: "/settings/business-profile", icon: Settings, labelKey: "settings", minRole: "admin" },
  ];

  const superAdminItems: NavItem[] = [
    { href: "/clients", icon: Shield, labelKey: "clients" },
    { href: "/celery", icon: Server, labelKey: "celery" },
    { href: "/system", icon: Activity, labelKey: "system" },
  ];

  const filteredItems = navItems.filter(
    (item) => !item.minRole || hasMinRole(item.minRole)
  );

  const SidebarContent = () => (
    <nav className="flex flex-col h-full" role="navigation" aria-label={t("main")}>
      {/* Logo */}
      <div className="flex items-center h-16 px-4 border-b border-border">
        <Link href="/" className="flex items-center gap-2">
          <div className="h-8 w-8 rounded-lg bg-primary flex items-center justify-center">
            <MessageSquare className="h-4 w-4 text-primary-foreground" />
          </div>
          {!sidebarCollapsed && (
            <span className="font-semibold text-lg">ConversaAI</span>
          )}
        </Link>
      </div>

      {/* Navegación principal */}
      <div className="flex-1 overflow-y-auto py-4 px-2 space-y-1">
        {filteredItems.map((item) => (
          <SidebarItem
            key={item.href}
            href={item.href}
            icon={item.icon}
            label={t(item.labelKey)}
            active={pathname === item.href || pathname.startsWith(item.href + "/")}
            collapsed={sidebarCollapsed}
            badge={item.badge}
          />
        ))}

        {/* Sección Super Admin */}
        {hasMinRole("super_admin") && (
          <>
            <div className="my-4 border-t border-border" />
            {!sidebarCollapsed && (
              <p className="px-3 mb-2 text-xs font-medium text-muted-foreground uppercase tracking-wider">
                {t("superAdmin")}
              </p>
            )}
            {superAdminItems.map((item) => (
              <SidebarItem
                key={item.href}
                href={item.href}
                icon={item.icon}
                label={t(item.labelKey)}
                active={pathname.startsWith(item.href)}
                collapsed={sidebarCollapsed}
              />
            ))}
          </>
        )}
      </div>

      {/* Botón de colapsar (solo desktop) */}
      <div className="hidden lg:flex border-t border-border p-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
          className="w-full justify-center"
          aria-label={sidebarCollapsed ? t("expand") : t("collapse")}
        >
          <ChevronLeft
            className={cn("h-4 w-4 transition-transform", sidebarCollapsed && "rotate-180")}
          />
        </Button>
      </div>
    </nav>
  );

  return (
    <>
      {/* Mobile: Sheet overlay */}
      <Sheet open={sidebarOpen} onOpenChange={setSidebarOpen}>
        <SheetTrigger asChild className="lg:hidden">
          <Button
            variant="ghost"
            size="icon"
            className="fixed top-4 left-4 z-40"
            aria-label={t("openMenu")}
          >
            <Menu className="h-5 w-5" />
          </Button>
        </SheetTrigger>
        <SheetContent side="left" className="w-72 p-0">
          <SidebarContent />
        </SheetContent>
      </Sheet>

      {/* Desktop: Sidebar fijo */}
      <aside
        className={cn(
          "hidden lg:flex flex-col border-r border-border bg-card transition-all duration-300",
          sidebarCollapsed ? "w-16" : "w-64"
        )}
      >
        <SidebarContent />
      </aside>
    </>
  );
}
```

**4.2 Header — `src/components/layout/Header.tsx`**

```typescript
"use client";

import { ThemeToggle } from "./ThemeToggle";
import { LanguageSelector } from "./LanguageSelector";
import { UserMenu } from "./UserMenu";
import { Breadcrumbs } from "./Breadcrumbs";
import { useTranslations } from "next-intl";

export function Header() {
  const t = useTranslations("common");

  return (
    <header
      className="h-16 border-b border-border bg-card flex items-center justify-between px-4 lg:px-6"
      role="banner"
    >
      {/* Breadcrumbs - oculto en móvil */}
      <div className="hidden md:block">
        <Breadcrumbs />
      </div>

      {/* Espaciador móvil para centrar acciones */}
      <div className="md:hidden" />

      {/* Acciones */}
      <div className="flex items-center gap-2">
        <LanguageSelector />
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}
```

**4.3 Dashboard Layout — `src/app/(dashboard)/layout.tsx`**

```typescript
import { Sidebar } from "@/components/layout/Sidebar";
import { Header } from "@/components/layout/Header";
import { ProtectedRoute } from "@/components/auth/ProtectedRoute";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <ProtectedRoute>
      <div className="flex h-screen overflow-hidden">
        <Sidebar />
        <div className="flex-1 flex flex-col overflow-hidden">
          <Header />
          <main className="flex-1 overflow-y-auto p-4 lg:p-6">
            {children}
          </main>
        </div>
      </div>
    </ProtectedRoute>
  );
}
```

Consideraciones responsive:
- **Móvil (<768px)**: Sidebar oculto, hamburger menu en la esquina. Header simplificado.
- **Tablet (768-1024px)**: Sidebar colapsado (solo íconos). Header con breadcrumbs.
- **Desktop (>1024px)**: Sidebar expandido (íconos + texto). Header completo.

---

### 5. Onboarding de Clientes — `src/components/auth/OnboardingWizard.tsx`

Página pública en `/onboarding` para el auto-registro de nuevos clientes/tenants.

**5.1 Esquemas de validación Zod — `src/lib/schemas/onboarding.ts`**

```typescript
import { z } from "zod";

export const businessInfoSchema = z.object({
  business_name: z
    .string()
    .min(2, "El nombre debe tener al menos 2 caracteres")
    .max(100, "El nombre no puede exceder 100 caracteres"),
  business_type: z.enum([
    "restaurant", "retail", "healthcare", "education",
    "real_estate", "professional_services", "other",
  ]),
  description: z
    .string()
    .max(500, "La descripción no puede exceder 500 caracteres")
    .optional(),
});

export const adminAccountSchema = z.object({
  email: z
    .string()
    .email("Correo electrónico inválido"),
  password: z
    .string()
    .min(8, "La contraseña debe tener al menos 8 caracteres")
    .regex(/[A-Z]/, "Debe contener al menos una mayúscula")
    .regex(/[0-9]/, "Debe contener al menos un número")
    .regex(/[^A-Za-z0-9]/, "Debe contener al menos un carácter especial"),
  password_confirm: z.string(),
  full_name: z
    .string()
    .min(2, "El nombre debe tener al menos 2 caracteres"),
  phone: z
    .string()
    .regex(/^\+?[1-9]\d{6,14}$/, "Número de teléfono inválido")
    .optional(),
}).refine((data) => data.password === data.password_confirm, {
  message: "Las contraseñas no coinciden",
  path: ["password_confirm"],
});

export const preferencesSchema = z.object({
  country: z.string().min(2, "Seleccione un país"),
  language: z.enum(["es", "en", "pt", "it", "de", "fr"]),
  timezone: z.string().min(1, "Seleccione una zona horaria"),
});

export const termsSchema = z.object({
  accept_terms: z.literal(true, {
    errorMap: () => ({ message: "Debe aceptar los términos y condiciones" }),
  }),
  accept_privacy: z.literal(true, {
    errorMap: () => ({ message: "Debe aceptar la política de privacidad" }),
  }),
});

export type BusinessInfoData = z.infer<typeof businessInfoSchema>;
export type AdminAccountData = z.infer<typeof adminAccountSchema>;
export type PreferencesData = z.infer<typeof preferencesSchema>;
export type TermsData = z.infer<typeof termsSchema>;
```

**5.2 Componente wizard multi-paso — `src/components/auth/OnboardingWizard.tsx`**

```typescript
"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Building2, UserPlus, Globe, FileCheck, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { apiPost } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useTranslations } from "next-intl";
import {
  businessInfoSchema, adminAccountSchema, preferencesSchema, termsSchema,
  type BusinessInfoData, type AdminAccountData, type PreferencesData, type TermsData,
} from "@/lib/schemas/onboarding";

const STEPS = [
  { id: 1, icon: Building2, labelKey: "onboarding.step1" },
  { id: 2, icon: UserPlus, labelKey: "onboarding.step2" },
  { id: 3, icon: Globe, labelKey: "onboarding.step3" },
  { id: 4, icon: FileCheck, labelKey: "onboarding.step4" },
] as const;

interface OnboardingData {
  business: BusinessInfoData;
  admin: AdminAccountData;
  preferences: PreferencesData;
  terms: TermsData;
}

export function OnboardingWizard() {
  const t = useTranslations("onboarding");
  const router = useRouter();
  const [currentStep, setCurrentStep] = useState(1);
  const [formData, setFormData] = useState<Partial<OnboardingData>>({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleStepSubmit = async (stepData: Record<string, unknown>) => {
    const stepKeys: (keyof OnboardingData)[] = ["business", "admin", "preferences", "terms"];
    const key = stepKeys[currentStep - 1];

    const updatedData = { ...formData, [key]: stepData };
    setFormData(updatedData);

    if (currentStep < 4) {
      setCurrentStep(currentStep + 1);
      return;
    }

    // Paso final: enviar todo al backend
    setIsSubmitting(true);
    setError(null);

    try {
      await apiPost("/onboarding/register", {
        business_name: updatedData.business!.business_name,
        business_type: updatedData.business!.business_type,
        description: updatedData.business!.description,
        admin_email: updatedData.admin!.email,
        admin_password: updatedData.admin!.password,
        admin_full_name: updatedData.admin!.full_name,
        admin_phone: updatedData.admin!.phone,
        country: updatedData.preferences!.country,
        language: updatedData.preferences!.language,
        timezone: updatedData.preferences!.timezone,
      });

      // Redirigir al login con mensaje de éxito
      router.push("/login?registered=true");
    } catch (err: unknown) {
      const message = (err as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail || t("errors.generic");
      setError(message);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-4 bg-background">
      <Card className="w-full max-w-lg">
        {/* Indicador de pasos */}
        <CardHeader>
          <div className="flex justify-between mb-6" role="progressbar" aria-valuenow={currentStep} aria-valuemin={1} aria-valuemax={4}>
            {STEPS.map((step) => {
              const StepIcon = step.icon;
              const isCompleted = currentStep > step.id;
              const isCurrent = currentStep === step.id;

              return (
                <div key={step.id} className="flex flex-col items-center gap-2 flex-1">
                  <div
                    className={cn(
                      "w-10 h-10 rounded-full flex items-center justify-center transition-colors",
                      isCompleted && "bg-primary text-primary-foreground",
                      isCurrent && "bg-primary/20 text-primary border-2 border-primary",
                      !isCompleted && !isCurrent && "bg-muted text-muted-foreground"
                    )}
                  >
                    {isCompleted ? (
                      <Check className="h-5 w-5" />
                    ) : (
                      <StepIcon className="h-5 w-5" />
                    )}
                  </div>
                  <span className={cn(
                    "text-xs text-center hidden sm:block",
                    isCurrent ? "text-primary font-medium" : "text-muted-foreground"
                  )}>
                    {t(step.labelKey)}
                  </span>
                </div>
              );
            })}
          </div>
          <CardTitle>{t(`steps.${currentStep}.title`)}</CardTitle>
        </CardHeader>

        <CardContent>
          {error && (
            <div className="mb-4 p-3 rounded-md bg-destructive/10 text-destructive text-sm" role="alert">
              {error}
            </div>
          )}

          {/* Renderizar formulario del paso actual */}
          {currentStep === 1 && (
            <BusinessInfoStep
              defaultValues={formData.business}
              onSubmit={handleStepSubmit}
            />
          )}
          {currentStep === 2 && (
            <AdminAccountStep
              defaultValues={formData.admin}
              onSubmit={handleStepSubmit}
              onBack={() => setCurrentStep(1)}
            />
          )}
          {currentStep === 3 && (
            <PreferencesStep
              defaultValues={formData.preferences}
              onSubmit={handleStepSubmit}
              onBack={() => setCurrentStep(2)}
            />
          )}
          {currentStep === 4 && (
            <TermsStep
              onSubmit={handleStepSubmit}
              onBack={() => setCurrentStep(3)}
              isSubmitting={isSubmitting}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
```

Cada sub-componente de paso (`BusinessInfoStep`, `AdminAccountStep`, `PreferencesStep`, `TermsStep`) utiliza `react-hook-form` con `zodResolver` y el esquema correspondiente. Patrón:

```typescript
function BusinessInfoStep({
  defaultValues,
  onSubmit,
}: {
  defaultValues?: BusinessInfoData;
  onSubmit: (data: BusinessInfoData) => void;
}) {
  const t = useTranslations("onboarding");
  const form = useForm<BusinessInfoData>({
    resolver: zodResolver(businessInfoSchema),
    defaultValues,
  });

  return (
    <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
      <div className="space-y-2">
        <Label htmlFor="business_name">{t("fields.businessName")}</Label>
        <Input
          id="business_name"
          {...form.register("business_name")}
          aria-invalid={!!form.formState.errors.business_name}
        />
        {form.formState.errors.business_name && (
          <p className="text-sm text-destructive">
            {form.formState.errors.business_name.message}
          </p>
        )}
      </div>
      {/* ... campos adicionales ... */}
      <Button type="submit" className="w-full">
        {t("actions.next")}
      </Button>
    </form>
  );
}
```

---

### 6. Personalización del Negocio — `src/components/settings/BusinessProfileForm.tsx`

**6.1 Hook de perfil — `src/hooks/useBusinessProfile.ts`**

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPut, api } from "@/lib/api";
import type { BusinessProfile } from "@/types/client";

export function useBusinessProfile() {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["businessProfile"],
    queryFn: () => apiGet<BusinessProfile>("/admin/business-profile"),
    staleTime: 10 * 60 * 1000,
  });

  const updateMutation = useMutation({
    mutationFn: (data: Partial<BusinessProfile>) =>
      apiPut<BusinessProfile>("/admin/business-profile", data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["businessProfile"] });
    },
  });

  const uploadLogoMutation = useMutation({
    mutationFn: async (file: File) => {
      const formData = new FormData();
      formData.append("logo", file);
      const response = await api.post("/admin/business-profile/logo", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["businessProfile"] });
    },
  });

  return {
    profile: query.data,
    isLoading: query.isLoading,
    error: query.error,
    updateProfile: updateMutation.mutateAsync,
    isUpdating: updateMutation.isPending,
    uploadLogo: uploadLogoMutation.mutateAsync,
    isUploadingLogo: uploadLogoMutation.isPending,
  };
}
```

**6.2 Editor de horarios — `src/components/settings/OperatingHoursEditor.tsx`**

```typescript
"use client";

import { useTranslations } from "next-intl";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface DaySchedule {
  enabled: boolean;
  open: string;   // "HH:mm"
  close: string;  // "HH:mm"
}

interface OperatingHoursEditorProps {
  value: Record<string, DaySchedule>;
  onChange: (hours: Record<string, DaySchedule>) => void;
}

const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

export function OperatingHoursEditor({ value, onChange }: OperatingHoursEditorProps) {
  const t = useTranslations("settings.hours");

  const updateDay = (day: string, field: keyof DaySchedule, fieldValue: string | boolean) => {
    onChange({
      ...value,
      [day]: { ...value[day], [field]: fieldValue },
    });
  };

  return (
    <div className="space-y-3">
      {DAYS.map((day) => (
        <div
          key={day}
          className="flex items-center gap-4 p-3 rounded-lg border border-border"
        >
          <Switch
            checked={value[day]?.enabled ?? false}
            onCheckedChange={(checked) => updateDay(day, "enabled", checked)}
            aria-label={t(`days.${day}`)}
          />
          <span className="w-24 text-sm font-medium">{t(`days.${day}`)}</span>
          <div className="flex items-center gap-2 flex-1">
            <Input
              type="time"
              value={value[day]?.open ?? "09:00"}
              onChange={(e) => updateDay(day, "open", e.target.value)}
              disabled={!value[day]?.enabled}
              className="w-28"
              aria-label={`${t(`days.${day}`)} - ${t("openTime")}`}
            />
            <span className="text-muted-foreground">-</span>
            <Input
              type="time"
              value={value[day]?.close ?? "18:00"}
              onChange={(e) => updateDay(day, "close", e.target.value)}
              disabled={!value[day]?.enabled}
              className="w-28"
              aria-label={`${t(`days.${day}`)} - ${t("closeTime")}`}
            />
          </div>
        </div>
      ))}
    </div>
  );
}
```

**6.3 Gestión de documentos (Knowledge Base) — `src/hooks/useDocuments.ts`**

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiDelete, api } from "@/lib/api";
import type { Document, PaginatedResponse } from "@/types";

interface DocumentFilters {
  page?: number;
  per_page?: number;
  status?: string;
  search?: string;
}

export function useDocuments(filters: DocumentFilters = {}) {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["documents", filters],
    queryFn: () =>
      apiGet<PaginatedResponse<Document>>("/documents", {
        page: filters.page ?? 1,
        per_page: filters.per_page ?? 20,
        ...(filters.status && { status: filters.status }),
        ...(filters.search && { search: filters.search }),
      }),
  });

  const uploadMutation = useMutation({
    mutationFn: async (files: File[]) => {
      const formData = new FormData();
      files.forEach((file) => formData.append("files", file));
      const response = await api.post("/documents/upload", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (documentId: string) => apiDelete(`/documents/${documentId}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
  });

  return {
    documents: query.data,
    isLoading: query.isLoading,
    uploadDocuments: uploadMutation.mutateAsync,
    isUploading: uploadMutation.isPending,
    uploadProgress: uploadMutation,
    deleteDocument: deleteMutation.mutateAsync,
    isDeleting: deleteMutation.isPending,
  };
}
```

**6.4 Componente de upload con drag-and-drop — `src/components/documents/DocumentUpload.tsx`**

```typescript
"use client";

import { useCallback } from "react";
import { useDropzone } from "react-dropzone";
import { Upload, FileText, X } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTranslations } from "next-intl";

const ACCEPTED_TYPES = {
  "application/pdf": [".pdf"],
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
  "text/plain": [".txt"],
  "text/csv": [".csv"],
};

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB

interface DocumentUploadProps {
  onUpload: (files: File[]) => Promise<void>;
  isUploading: boolean;
}

export function DocumentUpload({ onUpload, isUploading }: DocumentUploadProps) {
  const t = useTranslations("documents");

  const onDrop = useCallback(
    async (acceptedFiles: File[]) => {
      if (acceptedFiles.length > 0) {
        await onUpload(acceptedFiles);
      }
    },
    [onUpload]
  );

  const { getRootProps, getInputProps, isDragActive, fileRejections } = useDropzone({
    onDrop,
    accept: ACCEPTED_TYPES,
    maxSize: MAX_FILE_SIZE,
    maxFiles: 10,
    disabled: isUploading,
  });

  return (
    <div>
      <div
        {...getRootProps()}
        className={cn(
          "border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors",
          isDragActive
            ? "border-primary bg-primary/5"
            : "border-border hover:border-primary/50",
          isUploading && "opacity-50 cursor-not-allowed"
        )}
        role="button"
        aria-label={t("dropzone.label")}
        tabIndex={0}
      >
        <input {...getInputProps()} />
        <Upload className="h-10 w-10 mx-auto mb-4 text-muted-foreground" />
        <p className="text-sm font-medium">
          {isDragActive ? t("dropzone.active") : t("dropzone.idle")}
        </p>
        <p className="text-xs text-muted-foreground mt-1">
          {t("dropzone.formats")} - {t("dropzone.maxSize", { size: "10MB" })}
        </p>
      </div>

      {fileRejections.length > 0 && (
        <div className="mt-2 text-sm text-destructive" role="alert">
          {fileRejections.map(({ file, errors }) => (
            <p key={file.name}>
              {file.name}: {errors.map((e) => e.message).join(", ")}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}
```

---

### 7. Multi-idioma (6 Idiomas) — Configuración de next-intl

**7.1 Configuración — `src/i18n/config.ts`**

```typescript
export const locales = ["es", "en", "pt", "it", "de", "fr"] as const;
export type Locale = (typeof locales)[number];

export const defaultLocale: Locale = "es";

export const localeNames: Record<Locale, string> = {
  es: "Español",
  en: "English",
  pt: "Portugues",
  it: "Italiano",
  de: "Deutsch",
  fr: "Francais",
};

// Banderas (emoji) por idioma
export const localeFlags: Record<Locale, string> = {
  es: "\u{1F1EA}\u{1F1F8}",
  en: "\u{1F1FA}\u{1F1F8}",
  pt: "\u{1F1E7}\u{1F1F7}",
  it: "\u{1F1EE}\u{1F1F9}",
  de: "\u{1F1E9}\u{1F1EA}",
  fr: "\u{1F1EB}\u{1F1F7}",
};

// Formatos de fecha/número por locale
export const localeFormats: Record<Locale, { dateStyle: string; currency: string }> = {
  es: { dateStyle: "dd/MM/yyyy", currency: "EUR" },
  en: { dateStyle: "MM/dd/yyyy", currency: "USD" },
  pt: { dateStyle: "dd/MM/yyyy", currency: "BRL" },
  it: { dateStyle: "dd/MM/yyyy", currency: "EUR" },
  de: { dateStyle: "dd.MM.yyyy", currency: "EUR" },
  fr: { dateStyle: "dd/MM/yyyy", currency: "EUR" },
};
```

**7.2 Middleware para i18n — `src/middleware.ts`**

```typescript
import { NextRequest, NextResponse } from "next/server";
import { locales, defaultLocale } from "@/i18n/config";

const PUBLIC_PATHS = ["/login", "/onboarding", "/api"];

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // No aplicar a rutas públicas ni archivos estáticos
  if (
    PUBLIC_PATHS.some((p) => pathname.startsWith(p)) ||
    pathname.includes(".") ||
    pathname.startsWith("/_next")
  ) {
    return NextResponse.next();
  }

  // Verificar autenticación
  const token = request.cookies.get("access_token")?.value;
  if (!token && !PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("redirect", pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
```

**7.3 Selector de idioma — `src/components/layout/LanguageSelector.tsx`**

```typescript
"use client";

import { useTranslations } from "next-intl";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { Globe } from "lucide-react";
import { locales, localeNames, localeFlags, type Locale } from "@/i18n/config";

interface LanguageSelectorProps {
  currentLocale: Locale;
  onLocaleChange: (locale: Locale) => void;
}

export function LanguageSelector({ currentLocale, onLocaleChange }: LanguageSelectorProps) {
  const t = useTranslations("common");

  const handleChange = async (locale: Locale) => {
    // Actualizar preferencia en la API del backend
    try {
      await apiPut("/users/me/preferences", { language: locale });
    } catch {
      // Guardar en localStorage como fallback
    }
    localStorage.setItem("preferred-locale", locale);
    onLocaleChange(locale);
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className="gap-2" aria-label={t("language.select")}>
          <Globe className="h-4 w-4" />
          <span className="hidden sm:inline">{localeFlags[currentLocale]}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {locales.map((locale) => (
          <DropdownMenuItem
            key={locale}
            onClick={() => handleChange(locale)}
            className={currentLocale === locale ? "bg-accent" : ""}
          >
            <span className="mr-2">{localeFlags[locale]}</span>
            {localeNames[locale]}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
```

**7.4 Estructura de archivos de traducción — `public/locales/es/common.json`**

```json
{
  "nav": {
    "main": "Navegación principal",
    "dashboard": "Dashboard",
    "conversations": "Conversaciones",
    "contacts": "Contactos",
    "documents": "Documentos",
    "analytics": "Analítica",
    "settings": "Configuración",
    "superAdmin": "Super Admin",
    "clients": "Clientes",
    "celery": "Celery/Redis",
    "system": "Sistema",
    "expand": "Expandir menú",
    "collapse": "Colapsar menú",
    "openMenu": "Abrir menú"
  },
  "common": {
    "actions": {
      "save": "Guardar",
      "cancel": "Cancelar",
      "delete": "Eliminar",
      "edit": "Editar",
      "create": "Crear",
      "search": "Buscar",
      "filter": "Filtrar",
      "export": "Exportar",
      "import": "Importar",
      "confirm": "Confirmar",
      "back": "Volver",
      "next": "Siguiente",
      "close": "Cerrar",
      "retry": "Reintentar",
      "loading": "Cargando..."
    },
    "theme": {
      "toggle": "Cambiar tema",
      "light": "Claro",
      "dark": "Oscuro",
      "system": "Sistema"
    },
    "language": {
      "select": "Seleccionar idioma"
    },
    "status": {
      "active": "Activo",
      "inactive": "Inactivo",
      "pending": "Pendiente",
      "error": "Error",
      "success": "Éxito"
    },
    "notifications": {
      "saved": "Cambios guardados correctamente",
      "deleted": "Elemento eliminado correctamente",
      "error": "Ha ocurrido un error. Intente nuevamente.",
      "unauthorized": "No tiene permisos para esta acción",
      "sessionExpired": "Su sesión ha expirado. Inicie sesión nuevamente."
    },
    "pagination": {
      "previous": "Anterior",
      "next": "Siguiente",
      "showing": "Mostrando {from} a {to} de {total}",
      "perPage": "Por página"
    },
    "confirm": {
      "title": "Confirmar acción",
      "deleteMessage": "¿Está seguro que desea eliminar este elemento? Esta acción no se puede deshacer.",
      "yes": "Sí, confirmar",
      "no": "No, cancelar"
    }
  },
  "auth": {
    "login": {
      "title": "Iniciar sesión",
      "email": "Correo electrónico",
      "password": "Contraseña",
      "submit": "Ingresar",
      "forgotPassword": "¿Olvidó su contraseña?",
      "noAccount": "¿No tiene cuenta?",
      "register": "Regístrese aquí",
      "invalidCredentials": "Correo o contraseña incorrectos",
      "accountDisabled": "Su cuenta ha sido deshabilitada"
    },
    "logout": "Cerrar sesión"
  },
  "onboarding": {
    "step1": "Negocio",
    "step2": "Cuenta",
    "step3": "Preferencias",
    "step4": "Términos",
    "steps": {
      "1": { "title": "Información del negocio" },
      "2": { "title": "Cuenta de administrador" },
      "3": { "title": "Preferencias de contacto" },
      "4": { "title": "Términos y condiciones" }
    },
    "fields": {
      "businessName": "Nombre del negocio",
      "businessType": "Tipo de negocio",
      "description": "Descripción"
    },
    "actions": {
      "next": "Siguiente",
      "back": "Anterior",
      "submit": "Crear cuenta"
    },
    "errors": {
      "generic": "Error al crear la cuenta. Intente nuevamente."
    }
  },
  "dashboard": {
    "title": "Dashboard",
    "metrics": {
      "activeConversations": "Conversaciones activas",
      "messagesToday": "Mensajes hoy",
      "tokenUsage": "Uso de tokens",
      "avgResponseTime": "Tiempo de respuesta promedio",
      "csatScore": "Puntuación CSAT",
      "totalContacts": "Contactos totales"
    },
    "charts": {
      "conversationsByChannel": "Conversaciones por canal",
      "messagesOverTime": "Mensajes en el tiempo",
      "last7Days": "Últimos 7 días"
    },
    "recentConversations": "Conversaciones recientes"
  },
  "conversations": {
    "title": "Conversaciones",
    "search": "Buscar conversaciones...",
    "filters": {
      "all": "Todas",
      "active": "Activas",
      "waiting": "En espera",
      "resolved": "Resueltas"
    },
    "status": {
      "new": "Nueva",
      "bot_active": "Bot activo",
      "human_active": "Agente activo",
      "waiting_human": "Esperando agente",
      "waiting_client": "Esperando cliente",
      "resolved": "Resuelta",
      "archived": "Archivada"
    },
    "chat": {
      "inputPlaceholder": "Escriba un mensaje...",
      "send": "Enviar",
      "takeOver": "Tomar conversación",
      "returnToBot": "Devolver al bot",
      "quickReplies": "Respuestas rápidas",
      "attachFile": "Adjuntar archivo",
      "noMessages": "No hay mensajes aún"
    }
  },
  "contacts": {
    "title": "Contactos",
    "search": "Buscar contactos...",
    "fields": {
      "name": "Nombre",
      "phone": "Teléfono",
      "email": "Email",
      "channel": "Canal",
      "lastActivity": "Última actividad",
      "tags": "Etiquetas"
    }
  },
  "documents": {
    "title": "Base de conocimiento",
    "dropzone": {
      "label": "Zona de carga de archivos",
      "idle": "Arrastre archivos aquí o haga clic para seleccionar",
      "active": "Suelte los archivos aquí",
      "formats": "PDF, DOCX, TXT, CSV",
      "maxSize": "Máximo {size} por archivo"
    },
    "status": {
      "pending": "Pendiente",
      "processing": "Procesando",
      "ready": "Listo",
      "failed": "Error"
    }
  },
  "settings": {
    "title": "Configuración",
    "sections": {
      "businessProfile": "Perfil del negocio",
      "agents": "Agentes IA",
      "channels": "Canales",
      "quickReplies": "Respuestas rápidas",
      "team": "Equipo",
      "preferences": "Preferencias"
    },
    "hours": {
      "title": "Horario de atención",
      "days": {
        "monday": "Lunes",
        "tuesday": "Martes",
        "wednesday": "Miércoles",
        "thursday": "Jueves",
        "friday": "Viernes",
        "saturday": "Sábado",
        "sunday": "Domingo"
      },
      "openTime": "Hora de apertura",
      "closeTime": "Hora de cierre"
    }
  },
  "analytics": {
    "title": "Analítica",
    "periods": {
      "today": "Hoy",
      "last7Days": "Últimos 7 días",
      "last30Days": "Últimos 30 días",
      "custom": "Personalizado"
    }
  },
  "superAdmin": {
    "clients": {
      "title": "Gestión de clientes",
      "search": "Buscar clientes...",
      "fields": {
        "name": "Nombre",
        "status": "Estado",
        "plan": "Plan",
        "messages": "Mensajes",
        "tokens": "Tokens",
        "createdAt": "Fecha de creación"
      },
      "actions": {
        "activate": "Activar",
        "deactivate": "Desactivar",
        "suspend": "Suspender"
      }
    },
    "celery": {
      "title": "Celery / Redis",
      "workers": "Workers",
      "queues": "Colas",
      "tasks": "Tareas",
      "redis": "Redis",
      "fields": {
        "hostname": "Hostname",
        "status": "Estado",
        "activeTasks": "Tareas activas",
        "cpuUsage": "CPU",
        "memUsage": "Memoria"
      }
    }
  }
}
```

Nota: los archivos de traducción para `en`, `pt`, `it`, `de` y `fr` siguen exactamente la misma estructura de claves, con los valores traducidos al idioma correspondiente. Se mantiene la consistencia de claves para evitar errores de runtime.

Preparación para RTL (soporte futuro de árabe):
- Usar propiedades lógicas de CSS (`margin-inline-start` en vez de `margin-left`).
- El atributo `dir` en `<html>` se establece según el locale.
- Los layouts de flexbox usan `gap` en lugar de margins direccionales.

---

### 8. Dashboard de Administración — `src/app/(dashboard)/page.tsx`

**8.1 Hook de métricas — `src/hooks/useDashboard.ts`**

```typescript
import { useQuery } from "@tanstack/react-query";
import { apiGet } from "@/lib/api";
import type { DashboardMetrics, ChartData } from "@/types/analytics";

export function useDashboard() {
  const metrics = useQuery({
    queryKey: ["dashboard", "metrics"],
    queryFn: () => apiGet<DashboardMetrics>("/analytics/dashboard"),
    refetchInterval: 60 * 1000, // Actualizar cada 60 segundos
  });

  const conversationsByChannel = useQuery({
    queryKey: ["dashboard", "conversations-by-channel"],
    queryFn: () =>
      apiGet<ChartData[]>("/analytics/conversations-by-channel"),
    staleTime: 5 * 60 * 1000,
  });

  const messagesOverTime = useQuery({
    queryKey: ["dashboard", "messages-over-time"],
    queryFn: () =>
      apiGet<ChartData[]>("/analytics/messages-over-time", { days: 7 }),
    staleTime: 5 * 60 * 1000,
  });

  return {
    metrics: metrics.data,
    isLoadingMetrics: metrics.isLoading,
    conversationsByChannel: conversationsByChannel.data,
    messagesOverTime: messagesOverTime.data,
    isLoadingCharts:
      conversationsByChannel.isLoading || messagesOverTime.isLoading,
  };
}
```

**8.2 Página del dashboard — `src/app/(dashboard)/page.tsx`**

```typescript
"use client";

import { useTranslations } from "next-intl";
import { MessageSquare, Clock, Coins, ThumbsUp, Users, Zap } from "lucide-react";
import { StatCard } from "@/components/dashboard/StatCard";
import { ConversationsByChannel } from "@/components/dashboard/ConversationsByChannel";
import { MessagesOverTime } from "@/components/dashboard/MessagesOverTime";
import { RecentConversations } from "@/components/dashboard/RecentConversations";
import { useDashboard } from "@/hooks/useDashboard";
import { Skeleton } from "@/components/ui/skeleton";
import { Progress } from "@/components/ui/progress";

export default function DashboardPage() {
  const t = useTranslations("dashboard");
  const { metrics, isLoadingMetrics, conversationsByChannel, messagesOverTime } =
    useDashboard();

  if (isLoadingMetrics) {
    return (
      <div className="space-y-6">
        <h1 className="text-2xl font-bold">{t("title")}</h1>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-32 rounded-lg" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t("title")}</h1>

      {/* Tarjetas de métricas */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
        <StatCard
          title={t("metrics.activeConversations")}
          value={metrics?.active_conversations ?? 0}
          icon={MessageSquare}
          trend={metrics?.conversations_trend}
        />
        <StatCard
          title={t("metrics.messagesToday")}
          value={metrics?.messages_today ?? 0}
          icon={Zap}
          trend={metrics?.messages_trend}
        />
        <StatCard
          title={t("metrics.tokenUsage")}
          value={`${metrics?.token_usage_percentage ?? 0}%`}
          icon={Coins}
          footer={
            <Progress
              value={metrics?.token_usage_percentage ?? 0}
              className="mt-2"
              aria-label={t("metrics.tokenUsage")}
            />
          }
        />
        <StatCard
          title={t("metrics.avgResponseTime")}
          value={`${metrics?.avg_response_time_seconds ?? 0}s`}
          icon={Clock}
        />
        <StatCard
          title={t("metrics.csatScore")}
          value={`${metrics?.csat_score ?? 0}/5`}
          icon={ThumbsUp}
        />
        <StatCard
          title={t("metrics.totalContacts")}
          value={metrics?.total_contacts ?? 0}
          icon={Users}
        />
      </div>

      {/* Gráficos */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <ConversationsByChannel data={conversationsByChannel ?? []} />
        <MessagesOverTime data={messagesOverTime ?? []} />
      </div>

      {/* Conversaciones recientes */}
      <RecentConversations />
    </div>
  );
}
```

**8.3 Componente StatCard — `src/components/dashboard/StatCard.tsx`**

```typescript
import { Card, CardContent } from "@/components/ui/card";
import { TrendingUp, TrendingDown } from "lucide-react";
import { cn } from "@/lib/utils";

interface StatCardProps {
  title: string;
  value: string | number;
  icon: React.ComponentType<{ className?: string }>;
  trend?: number; // Porcentaje de cambio
  footer?: React.ReactNode;
}

export function StatCard({ title, value, icon: Icon, trend, footer }: StatCardProps) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between mb-2">
          <p className="text-sm text-muted-foreground">{title}</p>
          <Icon className="h-4 w-4 text-muted-foreground" />
        </div>
        <div className="flex items-baseline gap-2">
          <p className="text-2xl font-bold">{value}</p>
          {trend !== undefined && (
            <span
              className={cn(
                "flex items-center text-xs font-medium",
                trend >= 0 ? "text-emerald-500" : "text-red-500"
              )}
            >
              {trend >= 0 ? (
                <TrendingUp className="h-3 w-3 mr-0.5" />
              ) : (
                <TrendingDown className="h-3 w-3 mr-0.5" />
              )}
              {Math.abs(trend)}%
            </span>
          )}
        </div>
        {footer}
      </CardContent>
    </Card>
  );
}
```

**8.4 Gráfico de conversaciones por canal — `src/components/dashboard/ConversationsByChannel.tsx`**

```typescript
"use client";

import { useTranslations } from "next-intl";
import {
  PieChart, Pie, Cell, ResponsiveContainer, Tooltip, Legend,
} from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

const CHANNEL_COLORS: Record<string, string> = {
  whatsapp: "#25D366",
  instagram: "#E4405F",
  facebook: "#1877F2",
  web: "#6366F1",
  voice: "#8B5CF6",
};

interface ChartData {
  channel: string;
  count: number;
}

export function ConversationsByChannel({ data }: { data: ChartData[] }) {
  const t = useTranslations("dashboard.charts");

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("conversationsByChannel")}</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="h-64" role="img" aria-label={t("conversationsByChannel")}>
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={data}
                cx="50%"
                cy="50%"
                innerRadius={60}
                outerRadius={90}
                dataKey="count"
                nameKey="channel"
                paddingAngle={2}
              >
                {data.map((entry) => (
                  <Cell
                    key={entry.channel}
                    fill={CHANNEL_COLORS[entry.channel] ?? "#6B7280"}
                  />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{
                  backgroundColor: "hsl(var(--card))",
                  borderColor: "hsl(var(--border))",
                  borderRadius: "0.5rem",
                }}
              />
              <Legend />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </CardContent>
    </Card>
  );
}
```

---

### 9. Vista de Conversaciones — `src/app/(dashboard)/conversations/page.tsx`

**9.1 Hook de conversaciones con Realtime — `src/hooks/useConversations.ts`**

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { apiGet, apiPost, apiPatch } from "@/lib/api";
import { supabase } from "@/lib/supabase";
import type {
  Conversation, Message, ConversationStatus, PaginatedResponse,
} from "@/types";

interface ConversationFilters {
  status?: ConversationStatus;
  channel?: string;
  assigned_to?: string;
  search?: string;
  page?: number;
  per_page?: number;
}

export function useConversations(filters: ConversationFilters = {}) {
  const queryClient = useQueryClient();

  // Lista de conversaciones
  const query = useQuery({
    queryKey: ["conversations", filters],
    queryFn: () =>
      apiGet<PaginatedResponse<Conversation>>("/conversations", filters),
  });

  // Suscripción Realtime para nuevos mensajes
  useEffect(() => {
    const channel = supabase
      .channel("conversations")
      .on(
        "postgres_changes",
        {
          event: "*",
          schema: "public",
          table: "messages",
        },
        (payload) => {
          // Invalidar queries relevantes
          queryClient.invalidateQueries({ queryKey: ["conversations"] });

          if (payload.new) {
            const msg = payload.new as Message;
            queryClient.invalidateQueries({
              queryKey: ["conversation", msg.conversation_id],
            });
          }
        }
      )
      .subscribe();

    return () => {
      supabase.removeChannel(channel);
    };
  }, [queryClient]);

  return {
    conversations: query.data,
    isLoading: query.isLoading,
    refetch: query.refetch,
  };
}

// Hook individual de conversación con mensajes
export function useConversation(conversationId: string) {
  const queryClient = useQueryClient();

  const conversation = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () => apiGet<Conversation>(`/conversations/${conversationId}`),
    enabled: !!conversationId,
  });

  const messages = useQuery({
    queryKey: ["conversation", conversationId, "messages"],
    queryFn: () =>
      apiGet<PaginatedResponse<Message>>(
        `/conversations/${conversationId}/messages`,
        { per_page: 100 }
      ),
    enabled: !!conversationId,
  });

  // Suscripción Realtime para esta conversación específica
  useEffect(() => {
    if (!conversationId) return;

    const channel = supabase
      .channel(`conversation:${conversationId}`)
      .on(
        "postgres_changes",
        {
          event: "INSERT",
          schema: "public",
          table: "messages",
          filter: `conversation_id=eq.${conversationId}`,
        },
        (payload) => {
          // Agregar mensaje nuevo al cache sin re-fetch
          queryClient.setQueryData(
            ["conversation", conversationId, "messages"],
            (old: PaginatedResponse<Message> | undefined) => {
              if (!old) return old;
              return {
                ...old,
                items: [...old.items, payload.new as Message],
                total: old.total + 1,
              };
            }
          );
        }
      )
      .subscribe();

    return () => {
      supabase.removeChannel(channel);
    };
  }, [conversationId, queryClient]);

  // Enviar mensaje (takeover humano)
  const sendMessage = useMutation({
    mutationFn: (data: { text: string; attachments?: File[] }) =>
      apiPost(`/conversations/${conversationId}/messages`, {
        text: data.text,
        direction: "outbound",
        sender_type: "human",
      }),
  });

  // Cambiar estado
  const updateStatus = useMutation({
    mutationFn: (status: ConversationStatus) =>
      apiPatch(`/conversations/${conversationId}`, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["conversation", conversationId] });
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });

  return {
    conversation: conversation.data,
    messages: messages.data?.items ?? [],
    isLoading: conversation.isLoading || messages.isLoading,
    sendMessage: sendMessage.mutateAsync,
    isSending: sendMessage.isPending,
    updateStatus: updateStatus.mutateAsync,
  };
}
```

**9.2 Vista de chat — `src/components/conversations/ChatView.tsx`**

```typescript
"use client";

import { useRef, useEffect } from "react";
import { useTranslations } from "next-intl";
import { useConversation } from "@/hooks/useConversations";
import { MessageBubble } from "./MessageBubble";
import { MessageInput } from "./MessageInput";
import { ConversationHeader } from "./ConversationHeader";
import { ContactSidebar } from "./ContactSidebar";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";

interface ChatViewProps {
  conversationId: string;
}

export function ChatView({ conversationId }: ChatViewProps) {
  const t = useTranslations("conversations.chat");
  const {
    conversation, messages, isLoading, sendMessage, isSending, updateStatus,
  } = useConversation(conversationId);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll al recibir nuevos mensajes
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages.length]);

  if (isLoading) {
    return (
      <div className="flex flex-col h-full">
        <Skeleton className="h-16" />
        <div className="flex-1 p-4 space-y-4">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className={`h-12 ${i % 2 === 0 ? "w-2/3" : "w-1/2 ml-auto"}`} />
          ))}
        </div>
      </div>
    );
  }

  if (!conversation) return null;

  return (
    <div className="flex h-full">
      {/* Chat principal */}
      <div className="flex-1 flex flex-col min-w-0">
        <ConversationHeader
          conversation={conversation}
          onStatusChange={updateStatus}
        />

        {/* Mensajes */}
        <ScrollArea ref={scrollRef} className="flex-1 p-4">
          {messages.length === 0 ? (
            <div className="flex items-center justify-center h-full text-muted-foreground">
              {t("noMessages")}
            </div>
          ) : (
            <div className="space-y-4" role="log" aria-label={t("inputPlaceholder")} aria-live="polite">
              {messages.map((message) => (
                <MessageBubble key={message.id} message={message} />
              ))}
            </div>
          )}
        </ScrollArea>

        {/* Input de mensaje */}
        <MessageInput
          onSend={sendMessage}
          isSending={isSending}
          disabled={conversation.status === "resolved" || conversation.status === "archived"}
        />
      </div>

      {/* Panel lateral de contacto - oculto en móvil */}
      <div className="hidden xl:block w-80 border-l border-border">
        <ContactSidebar contactId={conversation.contact_id} />
      </div>
    </div>
  );
}
```

**9.3 Burbuja de mensaje — `src/components/conversations/MessageBubble.tsx`**

```typescript
"use client";

import { format } from "date-fns";
import { es, enUS, pt, it, de, fr } from "date-fns/locale";
import { Check, CheckCheck, Bot, User as UserIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Message } from "@/types/message";

const DATE_LOCALES = { es, en: enUS, pt, it, de, fr };

interface MessageBubbleProps {
  message: Message;
}

export function MessageBubble({ message }: MessageBubbleProps) {
  const isOutbound = message.direction === "outbound";
  const isBot = message.sender_type === "bot";
  const isHuman = message.sender_type === "human";

  return (
    <div
      className={cn("flex gap-2 max-w-[85%]", isOutbound ? "ml-auto flex-row-reverse" : "")}
    >
      {/* Avatar */}
      <Avatar className="h-8 w-8 flex-shrink-0">
        <AvatarFallback className={cn(
          "text-xs",
          isBot ? "bg-violet-100 text-violet-700 dark:bg-violet-900 dark:text-violet-300" :
          isHuman ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900 dark:text-emerald-300" :
          "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
        )}>
          {isBot ? <Bot className="h-4 w-4" /> :
           isHuman ? <UserIcon className="h-4 w-4" /> :
           message.contact_name?.[0]?.toUpperCase() ?? "C"}
        </AvatarFallback>
      </Avatar>

      {/* Burbuja */}
      <div
        className={cn(
          "rounded-2xl px-4 py-2 text-sm",
          isOutbound
            ? "bg-primary text-primary-foreground rounded-br-md"
            : "bg-muted rounded-bl-md"
        )}
      >
        {/* Contenido del mensaje */}
        {message.text && <p className="whitespace-pre-wrap break-words">{message.text}</p>}

        {/* Adjuntos */}
        {message.media_url && (
          <div className="mt-2">
            {message.media_type?.startsWith("image") ? (
              <img
                src={message.media_url}
                alt={message.caption ?? "Imagen adjunta"}
                className="rounded-lg max-w-full max-h-64 object-cover"
                loading="lazy"
              />
            ) : (
              <a
                href={message.media_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 text-xs underline"
              >
                {message.caption ?? "Archivo adjunto"}
              </a>
            )}
          </div>
        )}

        {/* Metadata: hora + estado de lectura */}
        <div className={cn(
          "flex items-center gap-1 mt-1",
          isOutbound ? "justify-end" : "justify-start"
        )}>
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="text-[10px] opacity-60">
                {format(new Date(message.created_at), "HH:mm")}
              </span>
            </TooltipTrigger>
            <TooltipContent>
              {format(new Date(message.created_at), "PPpp", { locale: es })}
            </TooltipContent>
          </Tooltip>

          {isOutbound && (
            <span className="opacity-60">
              {message.read_at ? (
                <CheckCheck className="h-3 w-3 text-blue-400" />
              ) : (
                <Check className="h-3 w-3" />
              )}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
```

Consideraciones de la vista de conversaciones:
- **Móvil**: Solo se muestra la lista o el chat, nunca ambos. Navegación con botón "volver".
- **Tablet**: Split panel con lista colapsada (solo avatares).
- **Desktop**: Split panel completo: lista (320px) + chat + panel de contacto (320px).
- **Realtime**: Los mensajes nuevos aparecen instantáneamente via Supabase Realtime sin necesidad de refrescar.
- **Accesibilidad**: `role="log"` con `aria-live="polite"` para anunciar nuevos mensajes a lectores de pantalla.

---

### 10. Dashboard de Celery/Redis (Super Admin) — `src/app/(super-admin)/celery/page.tsx`

**10.1 Hook de Celery — `src/hooks/useCelery.ts`**

```typescript
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost } from "@/lib/api";
import type { WorkerStatus, QueueDepth, TaskInfo, RedisMetrics } from "@/types/celery";

export function useCelery() {
  const queryClient = useQueryClient();

  const workers = useQuery({
    queryKey: ["celery", "workers"],
    queryFn: () => apiGet<WorkerStatus[]>("/admin/celery/workers"),
    refetchInterval: 30 * 1000, // Auto-refresh cada 30 segundos
  });

  const queues = useQuery({
    queryKey: ["celery", "queues"],
    queryFn: () => apiGet<QueueDepth[]>("/admin/celery/queues"),
    refetchInterval: 30 * 1000,
  });

  const tasks = useQuery({
    queryKey: ["celery", "tasks"],
    queryFn: () =>
      apiGet<TaskInfo[]>("/admin/celery/tasks", { limit: 50 }),
    refetchInterval: 30 * 1000,
  });

  const redis = useQuery({
    queryKey: ["celery", "redis"],
    queryFn: () => apiGet<RedisMetrics>("/admin/celery/redis"),
    refetchInterval: 30 * 1000,
  });

  const revokeTask = useMutation({
    mutationFn: (taskId: string) =>
      apiPost(`/admin/celery/tasks/${taskId}/revoke`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["celery", "tasks"] });
    },
  });

  return {
    workers: workers.data ?? [],
    queues: queues.data ?? [],
    tasks: tasks.data ?? [],
    redis: redis.data,
    isLoading: workers.isLoading || queues.isLoading,
    revokeTask: revokeTask.mutateAsync,
    isRevoking: revokeTask.isPending,
  };
}
```

**10.2 Página de Celery — `src/app/(super-admin)/celery/page.tsx`**

```typescript
"use client";

import { useTranslations } from "next-intl";
import { useCelery } from "@/hooks/useCelery";
import { WorkerStatusTable } from "@/components/super-admin/WorkerStatusTable";
import { QueueDepthChart } from "@/components/super-admin/QueueDepthChart";
import { TaskHistoryTable } from "@/components/super-admin/TaskHistoryTable";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";

export default function CeleryPage() {
  const t = useTranslations("superAdmin.celery");
  const { workers, queues, tasks, redis, isLoading, revokeTask } = useCelery();

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t("title")}</h1>
        <Badge variant="outline" className="gap-1">
          <RefreshCw className="h-3 w-3 animate-spin" />
          Auto-refresh: 30s
        </Badge>
      </div>

      {/* Workers y Redis en grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Workers - 2 columnas */}
        <div className="lg:col-span-2">
          <WorkerStatusTable workers={workers} />
        </div>

        {/* Redis Memory - 1 columna */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">{t("redis")}</CardTitle>
          </CardHeader>
          <CardContent>
            {redis && (
              <div className="space-y-4">
                <div>
                  <div className="flex justify-between text-sm mb-1">
                    <span className="text-muted-foreground">Memoria usada</span>
                    <span className="font-medium">
                      {redis.used_memory_human} / {redis.maxmemory_human}
                    </span>
                  </div>
                  <Progress
                    value={redis.memory_usage_percentage}
                    aria-label="Uso de memoria Redis"
                  />
                </div>
                <div className="grid grid-cols-2 gap-2 text-sm">
                  <div>
                    <p className="text-muted-foreground">Clientes conectados</p>
                    <p className="font-medium">{redis.connected_clients}</p>
                  </div>
                  <div>
                    <p className="text-muted-foreground">Ops/seg</p>
                    <p className="font-medium">{redis.ops_per_sec}</p>
                  </div>
                  <div>
                    <p className="text-muted-foreground">Hit rate</p>
                    <p className="font-medium">{redis.hit_rate}%</p>
                  </div>
                  <div>
                    <p className="text-muted-foreground">Uptime</p>
                    <p className="font-medium">{redis.uptime_days}d</p>
                  </div>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Profundidad de colas */}
      <QueueDepthChart queues={queues} />

      {/* Historial de tareas */}
      <TaskHistoryTable
        tasks={tasks}
        onRevoke={revokeTask}
      />
    </div>
  );
}
```

**10.3 Tabla de workers — `src/components/super-admin/WorkerStatusTable.tsx`**

```typescript
"use client";

import { useTranslations } from "next-intl";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { WorkerStatus } from "@/types/celery";

export function WorkerStatusTable({ workers }: { workers: WorkerStatus[] }) {
  const t = useTranslations("superAdmin.celery");

  return (
    <div className="border rounded-lg overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{t("fields.hostname")}</TableHead>
            <TableHead>{t("fields.status")}</TableHead>
            <TableHead className="text-right">{t("fields.activeTasks")}</TableHead>
            <TableHead className="text-right">{t("fields.cpuUsage")}</TableHead>
            <TableHead className="text-right">{t("fields.memUsage")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {workers.map((worker) => (
            <TableRow key={worker.hostname}>
              <TableCell className="font-mono text-sm">{worker.hostname}</TableCell>
              <TableCell>
                <Badge
                  variant={worker.status === "online" ? "default" : "destructive"}
                  className={cn(
                    worker.status === "online" && "bg-emerald-500 hover:bg-emerald-600"
                  )}
                >
                  {worker.status}
                </Badge>
              </TableCell>
              <TableCell className="text-right">{worker.active_tasks}</TableCell>
              <TableCell className="text-right">{worker.cpu_usage}%</TableCell>
              <TableCell className="text-right">{worker.memory_mb} MB</TableCell>
            </TableRow>
          ))}
          {workers.length === 0 && (
            <TableRow>
              <TableCell colSpan={5} className="text-center text-muted-foreground py-8">
                No se encontraron workers activos
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}
```

---

### 11. Gestión de Clientes (Super Admin) — `src/app/(super-admin)/clients/page.tsx`

**11.1 Página de clientes — `src/app/(super-admin)/clients/page.tsx`**

```typescript
"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPatch } from "@/lib/api";
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Dialog, DialogContent, DialogDescription, DialogFooter,
  DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Search, Eye } from "lucide-react";
import type { Client, PaginatedResponse } from "@/types";

export default function ClientsPage() {
  const t = useTranslations("superAdmin.clients");
  const router = useRouter();
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [confirmDialog, setConfirmDialog] = useState<{
    open: boolean;
    clientId: string;
    action: "activate" | "deactivate";
  }>({ open: false, clientId: "", action: "activate" });

  const { data: clients, isLoading } = useQuery({
    queryKey: ["clients", search, statusFilter],
    queryFn: () =>
      apiGet<PaginatedResponse<Client>>("/super-admin/clients", {
        search: search || undefined,
        status: statusFilter !== "all" ? statusFilter : undefined,
      }),
  });

  const toggleStatus = useMutation({
    mutationFn: ({ clientId, active }: { clientId: string; active: boolean }) =>
      apiPatch(`/super-admin/clients/${clientId}`, { is_active: active }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["clients"] });
      setConfirmDialog({ open: false, clientId: "", action: "activate" });
    },
  });

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t("title")}</h1>

      {/* Filtros */}
      <div className="flex flex-col sm:flex-row gap-4">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder={t("search")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-10"
            aria-label={t("search")}
          />
        </div>
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="w-40" aria-label="Filtrar por estado">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{t("filters.all")}</SelectItem>
            <SelectItem value="active">{t("filters.active")}</SelectItem>
            <SelectItem value="inactive">{t("filters.inactive")}</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {/* Tabla de clientes */}
      <div className="border rounded-lg overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("fields.name")}</TableHead>
              <TableHead>{t("fields.status")}</TableHead>
              <TableHead className="text-right">{t("fields.messages")}</TableHead>
              <TableHead className="text-right">{t("fields.tokens")}</TableHead>
              <TableHead>{t("fields.createdAt")}</TableHead>
              <TableHead className="text-right">Acciones</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {clients?.items.map((client) => (
              <TableRow key={client.id}>
                <TableCell className="font-medium">{client.business_name}</TableCell>
                <TableCell>
                  <Badge variant={client.is_active ? "default" : "secondary"}>
                    {client.is_active ? t("status.active") : t("status.inactive")}
                  </Badge>
                </TableCell>
                <TableCell className="text-right">
                  {client.message_count?.toLocaleString()}
                </TableCell>
                <TableCell className="text-right">
                  {client.token_usage?.toLocaleString()}
                </TableCell>
                <TableCell>
                  {new Date(client.created_at).toLocaleDateString()}
                </TableCell>
                <TableCell className="text-right">
                  <div className="flex justify-end gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => router.push(`/clients/${client.id}`)}
                      aria-label={`Ver detalles de ${client.business_name}`}
                    >
                      <Eye className="h-4 w-4" />
                    </Button>
                    <Button
                      variant={client.is_active ? "destructive" : "default"}
                      size="sm"
                      onClick={() =>
                        setConfirmDialog({
                          open: true,
                          clientId: client.id,
                          action: client.is_active ? "deactivate" : "activate",
                        })
                      }
                    >
                      {client.is_active ? t("actions.deactivate") : t("actions.activate")}
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      {/* Diálogo de confirmación */}
      <Dialog
        open={confirmDialog.open}
        onOpenChange={(open) =>
          setConfirmDialog((prev) => ({ ...prev, open }))
        }
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t(`confirm.${confirmDialog.action}.title`)}</DialogTitle>
            <DialogDescription>
              {t(`confirm.${confirmDialog.action}.description`)}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() =>
                setConfirmDialog((prev) => ({ ...prev, open: false }))
              }
            >
              {t("actions.cancel")}
            </Button>
            <Button
              variant={confirmDialog.action === "deactivate" ? "destructive" : "default"}
              onClick={() =>
                toggleStatus.mutate({
                  clientId: confirmDialog.clientId,
                  active: confirmDialog.action === "activate",
                })
              }
              disabled={toggleStatus.isPending}
            >
              {t(`actions.${confirmDialog.action}`)}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
```

---

### 12. Tipos TypeScript — `src/types/`

**12.1 Tipos de autenticación — `src/types/auth.ts`**

```typescript
export type UserRole = "super_admin" | "admin" | "supervisor" | "agent";

export interface User {
  id: string;
  client_id: string;
  email: string;
  full_name: string;
  role: UserRole;
  is_active: boolean;
  phone?: string;
  avatar_url?: string;
  language: string;
  timezone: string;
  created_at: string;
  updated_at: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
}

export interface OnboardingRequest {
  business_name: string;
  business_type: string;
  description?: string;
  admin_email: string;
  admin_password: string;
  admin_full_name: string;
  admin_phone?: string;
  country: string;
  language: string;
  timezone: string;
}
```

**12.2 Tipos de conversación — `src/types/conversation.ts`**

```typescript
export type ConversationStatus =
  | "new"
  | "bot_active"
  | "human_active"
  | "waiting_human"
  | "waiting_client"
  | "resolved"
  | "archived";

export type Channel = "whatsapp" | "instagram" | "facebook" | "web" | "voice";

export interface Conversation {
  id: string;
  client_id: string;
  contact_id: string;
  contact_name: string;
  contact_avatar?: string;
  channel: Channel;
  status: ConversationStatus;
  assigned_user_id?: string;
  assigned_user_name?: string;
  last_message_text?: string;
  last_message_at: string;
  unread_count: number;
  detected_language?: string;
  sentiment?: "positive" | "neutral" | "negative";
  created_at: string;
  resolved_at?: string;
}
```

**12.3 Tipos de mensaje — `src/types/message.ts`**

```typescript
export type MessageDirection = "inbound" | "outbound";
export type SenderType = "contact" | "bot" | "human";
export type MessageType = "text" | "image" | "audio" | "video" | "document" | "location" | "interactive";

export interface Message {
  id: string;
  conversation_id: string;
  direction: MessageDirection;
  sender_type: SenderType;
  sender_name?: string;
  contact_name?: string;
  message_type: MessageType;
  text?: string;
  media_url?: string;
  media_type?: string;
  caption?: string;
  metadata?: Record<string, unknown>;
  read_at?: string;
  delivered_at?: string;
  created_at: string;
}
```

**12.4 Tipos de Celery — `src/types/celery.ts`**

```typescript
export interface WorkerStatus {
  hostname: string;
  status: "online" | "offline";
  active_tasks: number;
  cpu_usage: number;
  memory_mb: number;
  pid: number;
  last_heartbeat: string;
}

export interface QueueDepth {
  queue_name: string;
  pending_count: number;
  active_count: number;
}

export interface TaskInfo {
  id: string;
  name: string;
  status: "PENDING" | "STARTED" | "SUCCESS" | "FAILURE" | "REVOKED" | "RETRY";
  queue: string;
  args?: string;
  kwargs?: string;
  result?: string;
  exception?: string;
  started_at?: string;
  finished_at?: string;
  runtime_seconds?: number;
  retries: number;
}

export interface RedisMetrics {
  used_memory_human: string;
  maxmemory_human: string;
  memory_usage_percentage: number;
  connected_clients: number;
  ops_per_sec: number;
  hit_rate: number;
  uptime_days: number;
}
```

**12.5 Tipos comunes — `src/types/common.ts`**

```typescript
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
}

export interface ApiError {
  detail: string;
  status_code: number;
  errors?: Record<string, string[]>;
}

export interface SelectOption {
  value: string;
  label: string;
}
```

---

### 13. Guardia de Rutas con RBAC — `src/components/auth/ProtectedRoute.tsx`

```typescript
"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/authStore";
import type { UserRole } from "@/types/auth";
import { Skeleton } from "@/components/ui/skeleton";

interface ProtectedRouteProps {
  children: React.ReactNode;
  minRole?: UserRole;
}

export function ProtectedRoute({ children, minRole }: ProtectedRouteProps) {
  const router = useRouter();
  const { isAuthenticated, isLoading, hasMinRole, user } = useAuthStore();

  useEffect(() => {
    if (!isLoading && !isAuthenticated) {
      router.push(`/login?redirect=${window.location.pathname}`);
    }
  }, [isAuthenticated, isLoading, router]);

  useEffect(() => {
    if (!isLoading && isAuthenticated && minRole && !hasMinRole(minRole)) {
      router.push("/"); // Redirigir al dashboard si no tiene permisos
    }
  }, [isAuthenticated, isLoading, minRole, hasMinRole, router]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="space-y-4 w-full max-w-md">
          <Skeleton className="h-8 w-3/4" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      </div>
    );
  }

  if (!isAuthenticated) return null;
  if (minRole && !hasMinRole(minRole)) return null;

  return <>{children}</>;
}
```

---

### 14. Cliente Supabase Realtime — `src/lib/supabase.ts`

```typescript
import { createClient } from "@supabase/supabase-js";

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

export const supabase = createClient(supabaseUrl, supabaseAnonKey, {
  realtime: {
    params: {
      eventsPerSecond: 10,
    },
  },
  auth: {
    persistSession: false, // Usamos nuestro propio sistema de auth
    autoRefreshToken: false,
  },
});
```

---

### 15. Docker & Despliegue

**15.1 Dockerfile — `frontend/Dockerfile`**

```dockerfile
# Stage 1: Dependencias
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --only=production

# Stage 2: Build
FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Variables de build (inyectadas en build time)
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_SUPABASE_URL
ARG NEXT_PUBLIC_SUPABASE_ANON_KEY
ARG NEXT_PUBLIC_DEFAULT_LOCALE=es

ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# Stage 3: Runner
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Crear usuario no-root
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copiar artefactos de build standalone
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
```

**15.2 Docker Compose (servicio frontend) — agregar a `docker-compose.yml`**

```yaml
services:
  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
      args:
        NEXT_PUBLIC_API_URL: ${NEXT_PUBLIC_API_URL:-http://api:8000}
        NEXT_PUBLIC_SUPABASE_URL: ${NEXT_PUBLIC_SUPABASE_URL}
        NEXT_PUBLIC_SUPABASE_ANON_KEY: ${NEXT_PUBLIC_SUPABASE_ANON_KEY}
    restart: unless-stopped
    networks:
      - internal
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.frontend.rule=Host(`app.${DOMAIN}`)"
      - "traefik.http.routers.frontend.entrypoints=websecure"
      - "traefik.http.routers.frontend.tls.certresolver=letsencrypt"
      - "traefik.http.services.frontend.loadbalancer.server.port=3000"
    depends_on:
      - api
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:3000"]
      interval: 30s
      timeout: 10s
      retries: 3
```

**15.3 Configuración Traefik — rutas**

| Dominio | Servicio | Puerto |
|---|---|---|
| `app.yourdomain.com` | frontend | 3000 |
| `api.yourdomain.com` | api (FastAPI) | 8000 |
| `supabase.yourdomain.com` | supabase (Kong) | 8000 |

---

### 16. Utilidades — `src/lib/utils.ts`

```typescript
import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";
import { format, formatDistanceToNow } from "date-fns";
import { es, enUS, pt, it, de, fr } from "date-fns/locale";

// Merge de clases Tailwind
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// Locales de date-fns
const DATE_LOCALES: Record<string, Locale> = { es, en: enUS, pt, it, de, fr };

// Formatear fecha relativa ("hace 5 minutos")
export function timeAgo(date: string | Date, locale = "es"): string {
  return formatDistanceToNow(new Date(date), {
    addSuffix: true,
    locale: DATE_LOCALES[locale] ?? es,
  });
}

// Formatear fecha completa
export function formatDate(
  date: string | Date,
  formatStr = "dd/MM/yyyy HH:mm",
  locale = "es"
): string {
  return format(new Date(date), formatStr, {
    locale: DATE_LOCALES[locale] ?? es,
  });
}

// Truncar texto
export function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return text.slice(0, maxLength - 3) + "...";
}

// Formatear número con separador de miles
export function formatNumber(num: number, locale = "es"): string {
  return new Intl.NumberFormat(locale).format(num);
}

// Formatear bytes a legible
export function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

// Generar iniciales para avatar
export function getInitials(name: string): string {
  return name
    .split(" ")
    .map((n) => n[0])
    .join("")
    .toUpperCase()
    .slice(0, 2);
}

// Debounce para búsquedas
export function debounce<T extends (...args: unknown[]) => unknown>(
  func: T,
  wait: number
): (...args: Parameters<T>) => void {
  let timeout: ReturnType<typeof setTimeout>;
  return (...args: Parameters<T>) => {
    clearTimeout(timeout);
    timeout = setTimeout(() => func(...args), wait);
  };
}
```

---

## Criterios de Aceptación

| # | Criterio | Validación |
|---|---|---|
| 1 | Login funcional con JWT access + refresh tokens | Usuario puede iniciar sesión, el token se refresca automáticamente al expirar, y el logout limpia el estado |
| 2 | RBAC aplicado en el frontend | Cada ruta y componente respeta la jerarquía de roles: super_admin > admin > supervisor > agent. Rutas protegidas redirigen a login si no autenticado |
| 3 | Onboarding multi-paso completo | Un nuevo cliente puede registrarse a través del wizard de 4 pasos. Validación con Zod en cada paso. Datos enviados al endpoint del backend correctamente |
| 4 | Tema claro/oscuro/sistema funcional | Toggle con 3 opciones. Sin flash al cargar. Preferencia persistida en localStorage. Todos los componentes respetan el tema activo |
| 5 | Diseño responsive en 3 breakpoints | Mobile (<768px): sidebar como sheet. Tablet (768-1024): sidebar colapsado. Desktop (>1024): sidebar completo. Tablas con scroll horizontal en mobile |
| 6 | 6 idiomas funcionales | Selector de idioma en header. Todos los textos de la UI traducidos en es, en, pt, it, de, fr. Formato de fecha/número respeta el locale |
| 7 | Dashboard con métricas y gráficos | 6 tarjetas de métricas. Gráfico de pie (conversaciones por canal). Gráfico de líneas (mensajes en el tiempo). Auto-refresh cada 60s |
| 8 | Vista de conversaciones con Realtime | Lista de conversaciones con filtros. Chat view con mensajes en tiempo real via Supabase Realtime. Burbujas con avatar, hora, estado de lectura |
| 9 | Personalización del negocio | Formulario de perfil con upload de logo, horarios de atención, colores de marca, enlaces sociales. Mensajes de agente con preview |
| 10 | Gestión de knowledge base | Upload drag-and-drop de documentos (PDF, DOCX, TXT, CSV). Lista con estado de procesamiento. Búsqueda y eliminación |
| 11 | Panel de Celery/Redis (super_admin) | Tabla de workers, gráfico de colas, historial de tareas filtrable, métricas Redis con gauge. Auto-refresh cada 30s. Acción de revocar tarea |
| 12 | Gestión de clientes (super_admin) | Lista con búsqueda y filtro de estado. Detalle con estadísticas de uso. Toggle activar/desactivar con confirmación |
| 13 | Docker multi-stage build funcional | Build produce imagen standalone <200MB. Servicio frontend en Docker Compose con healthcheck. Traefik rutea correctamente |
| 14 | Accesibilidad básica implementada | Todos los formularios tienen labels. Botones tienen aria-labels. Navegación por teclado funcional. Contraste WCAG AA en ambos temas |
| 15 | Gestión de equipo funcional | Admin puede listar, crear, editar y desactivar usuarios de su tenant. Asignación de roles respeta la jerarquía |

## Notas Técnicas

- **Next.js App Router** se usa con componentes server y client. Las páginas que necesitan estado interactivo (formularios, gráficos, realtime) usan `"use client"`. Los layouts que solo envuelven proveedores son server components cuando es posible.
- **Zustand** se usa exclusivamente para estado global de UI (sidebar, modales) y autenticación. Todo el estado del servidor (conversaciones, contactos, métricas) se gestiona con **TanStack Query** para beneficiarse de cache, deduplicación, retry automático y invalidación.
- **React Hook Form + Zod** garantiza validación idéntica entre frontend y backend. Los esquemas Zod reflejan las validaciones Pydantic del backend.
- **Supabase Realtime** se conecta con el Supabase self-hosted del backend. Solo se usa para suscripciones a cambios en la tabla `messages`. La autenticación se mantiene separada (JWT propio).
- **next-themes** gestiona el tema con `class` strategy (agrega/quita clase `dark` en `<html>`), compatible con Tailwind CSS `darkMode: "class"`.
- **Interceptores de Axios**: el refresh automático de tokens es transparente. Si el refresh falla, el usuario es redirigido a login. Todas las requests incluyen el access token en el header `Authorization`.
- **SSR mínimo**: la mayoría de páginas son client-rendered dentro del App Router. El SSR se reserva para el shell (layouts) y páginas que no requieren autenticación (login, onboarding).
- **Rendimiento**: `React.lazy` y `dynamic import` de Next.js para componentes pesados (gráficos, editores). TanStack Query con `staleTime` configurable por query para reducir requests.
- **Seguridad**: tokens no se exponen en URLs. Las cookies `httpOnly` previenen XSS. El middleware de Next.js valida la existencia del token antes de servir rutas protegidas.
- **Preparación RTL**: se usan propiedades lógicas de CSS (inline-start/end en vez de left/right) donde es posible, para facilitar el soporte futuro de idiomas RTL como árabe.

## Dependencias

| Paquete npm | Versión | Propósito |
|---|---|---|
| `next` | ^14.2.0 | Framework React con App Router |
| `react` | ^18.3.0 | Biblioteca de UI |
| `react-dom` | ^18.3.0 | Renderizado DOM |
| `typescript` | ^5.4.0 | Tipado estático |
| `tailwindcss` | ^3.4.0 | Framework CSS utility-first |
| `tailwindcss-animate` | ^1.0.7 | Animaciones para shadcn/ui |
| `@radix-ui/react-*` | latest | Primitivos accesibles (via shadcn/ui) |
| `class-variance-authority` | ^0.7.0 | Variantes de componentes (shadcn) |
| `clsx` | ^2.1.0 | Merge condicional de clases |
| `tailwind-merge` | ^2.2.0 | Merge inteligente de clases Tailwind |
| `lucide-react` | latest | Biblioteca de íconos |
| `zustand` | ^4.5.0 | Estado global ligero |
| `@tanstack/react-query` | ^5.0.0 | Cache y sincronización de server state |
| `react-hook-form` | ^7.50.0 | Gestión de formularios performante |
| `@hookform/resolvers` | ^3.3.0 | Integración Zod con React Hook Form |
| `zod` | ^3.22.0 | Validación de esquemas |
| `axios` | ^1.7.0 | Cliente HTTP con interceptores |
| `next-themes` | ^0.3.0 | Gestión de tema claro/oscuro/sistema |
| `next-intl` | ^3.0.0 | Internacionalización para Next.js |
| `recharts` | ^2.12.0 | Gráficos React (pie, line, bar) |
| `@supabase/supabase-js` | ^2.40.0 | Cliente Supabase (Realtime) |
| `date-fns` | ^3.3.0 | Utilidades de fecha con i18n |
| `react-dropzone` | ^14.2.0 | Drag-and-drop de archivos |
| `sonner` | ^1.4.0 | Notificaciones toast |
| `cmdk` | ^0.2.0 | Command palette (Cmd+K) |

### Dependencias de desarrollo

| Paquete npm | Versión | Propósito |
|---|---|---|
| `@types/react` | ^18.3.0 | Tipos TypeScript para React |
| `@types/node` | ^20.0.0 | Tipos TypeScript para Node.js |
| `eslint` | ^8.56.0 | Linter JavaScript/TypeScript |
| `eslint-config-next` | ^14.2.0 | Config ESLint para Next.js |
| `prettier` | ^3.2.0 | Formateador de código |
| `prettier-plugin-tailwindcss` | ^0.5.0 | Ordenamiento de clases Tailwind |

## Dependencias para Sprint 16

El Sprint 16 puede construir sobre esta base frontend para agregar:

- **WebSocket nativo**: migrar de Supabase Realtime a WebSocket propio para notificaciones push en tiempo real y presencia de agentes.
- **PWA (Progressive Web App)**: service worker para notificaciones push, modo offline para visualización de conversaciones recientes, e instalación como app nativa.
- **Tests E2E**: suite de tests con Playwright cubriendo flujos críticos (login, onboarding, conversaciones, settings).
- **Storybook**: documentación visual interactiva de todos los componentes de shadcn/ui personalizados.
- **Analítica avanzada**: dashboards con filtros de fecha personalizados, exportación a CSV/PDF, y comparativas de periodos.
- **Editor de flujos de agentes**: interfaz visual tipo "node editor" para configurar el grafo de LangGraph sin código.
- **Modo sandbox visual**: toggle en el header para cambiar entre producción y sandbox, con indicador visual claro del entorno activo.
