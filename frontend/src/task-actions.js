export function taskPageParams(page, pageSize) {
  return { page, page_size: pageSize }
}

export async function confirmAndDeleteTask(row, confirm, remove) {
  await confirm(row)
  await remove(row)
}
