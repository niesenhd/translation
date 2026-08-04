/** Persist a v-model boolean change and restore the old value if persistence fails. */
export async function persistBooleanToggle(row, key, persist) {
  const nextValue = Boolean(row[key])
  try {
    await persist(nextValue)
    return nextValue
  } catch (error) {
    row[key] = !nextValue
    throw error
  }
}
