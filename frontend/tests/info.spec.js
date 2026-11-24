import {test, expect} from '@playwright/test';

test('info board is a flat read-only list with safe markdown', async ({page}) => {
  await page.goto('/');
  await page.getByRole('button', {name: '信息台', exact: true}).click();
  await expect(page).toHaveURL(/\/info$/);
  await expect(page.getByRole('heading', {name: '信息台', exact: true})).toBeVisible();
  await expect(page.locator('.info-card').first()).toContainText('信息台浏览器演示');
  await expect(page.locator('.info-card').first()).toContainText('repo:ui-fixture');

  // Raw HTML is skipped: the payload never executes and no remote image loads.
  expect(await page.evaluate(() => window.__infoXss)).toBeUndefined();
  expect(await page.locator('img[src*="example.com"]').count()).toBe(0);

  await page.locator('.info-expand').first().click();
  await expect(page.locator('.info-markdown').first()).toContainText('平铺信息');
  await expect(page.locator('.info-image-placeholder').first()).toContainText('图片未自动加载');
  const link = page.locator('.info-markdown a', {hasText: '外部链接'}).first();
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', 'noreferrer');
  expect(await page.evaluate(() => window.__infoXss)).toBeUndefined();

  await page.getByLabel('搜索信息', {exact: true}).fill('不存在的关键词');
  await page.getByRole('button', {name: '搜索', exact: true}).click();
  await expect(page.getByText('没有匹配的信息')).toBeVisible();
  await page.getByRole('button', {name: '清除搜索', exact: true}).click();
  await expect(page.locator('.info-card').first()).toBeVisible();

  await page.setViewportSize({width: 390, height: 844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
});
