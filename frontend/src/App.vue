<script setup lang="ts">
// 全局壳: 暗色主题 + 中文 locale + 主布局
import { darkTheme, zhCN, dateZhCN, type GlobalThemeOverrides } from 'naive-ui'
import MainLayout from './layouts/MainLayout.vue'
import './styles/workspace.css'

// 暗色主题下未选中的开关轨/勾选框边框太暗, 几乎看不见 —— 提亮
const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#83b5ff', primaryColorHover: '#a4caff', primaryColorPressed: '#6398e4', primaryColorSuppl: '#83b5ff',
    infoColor: '#83b5ff', successColor: '#78c9a5', warningColor: '#efbd70', errorColor: '#ef9299',
    bodyColor: '#101318', cardColor: '#191e26', modalColor: '#1d242e', popoverColor: '#242d39',
    tableColor: '#191e26', inputColor: '#12171e', borderColor: '#303946', dividerColor: '#2a323e',
    textColorBase: '#edf2f8', textColor1: '#e6ebf2', textColor2: '#c3cdd9', textColor3: '#9aa7b8',
    borderRadius: '8px', fontSize: '14px', fontFamily: '"Segoe UI", "Microsoft YaHei", system-ui, sans-serif',
  },
  Layout: { color: '#101318', siderColor: '#151a21' },
  Card: { borderRadius: '12px', paddingSmall: '20px', titleFontSizeSmall: '15px' },
  Button: { fontWeight: '500', heightSmall: '32px', heightTiny: '26px' },
  Menu: { itemHeight: '36px', itemColorActive: '#25374d', itemColorActiveHover: '#2c415b', itemTextColorActive: '#c4ddff', itemIconColorActive: '#a4caff', borderRadius: '8px' },
  Tag: { borderRadius: '5px' },
  Switch: { railColor: '#4c5868' },
  Checkbox: { border: '1px solid #657287' },
}
</script>

<template>
  <n-config-provider :theme="darkTheme" :theme-overrides="themeOverrides"
                     :locale="zhCN" :date-locale="dateZhCN">
    <n-dialog-provider>
      <n-message-provider>
        <MainLayout />
      </n-message-provider>
    </n-dialog-provider>
  </n-config-provider>
</template>
