import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'layout',
    component: () => import('@/layouts/MainLayout.vue'),
    children: [
      {
        path: '',
        name: 'dashboard',
        component: () => import('@/views/Dashboard.vue'),
        meta: { title: '总览' }
      },
      {
        path: 'holdings',
        name: 'holdings',
        component: () => import('@/views/Holdings.vue'),
        meta: { title: '持仓' }
      },
      {
        path: 'cashflow',
        name: 'cashflow',
        component: () => import('@/views/Cashflow.vue'),
        meta: { title: '收支' }
      },
      {
        path: 'strategy',
        name: 'strategy',
        component: () => import('@/views/Strategy.vue'),
        meta: { title: '策略' }
      },
      {
        path: 'watchlist',
        name: 'watchlist',
        component: () => import('@/views/Watchlist.vue'),
        meta: { title: '观察池' }
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes
})

// Set page title
router.beforeEach((to, _from, next) => {
  document.title = to.meta.title ? `${to.meta.title} - PAMS` : 'PAMS'
  next()
})

export default router
