import { createRouter, createWebHashHistory } from 'vue-router'
import Home from '../views/Home.vue'
import SystemInfo from '../views/SystemInfo.vue'

const routes = [
  {
    path: '/',
    name: 'Home',
    component: Home
  },
  {
    path: '/system-info',
    name: 'SystemInfo',
    component: SystemInfo
  }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

export default router
