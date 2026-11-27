import { createRouter, createWebHistory } from 'vue-router'

import ArchiveMigrationView from '@/views/ArchiveMigrationView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/archive-migration', name: 'archive-migration', component: ArchiveMigrationView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
