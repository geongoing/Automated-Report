"""Current output names and the legacy names accepted during recovery."""


def hmi_stems(date):
    return (date.strftime('송도_HMI 중앙제어실 _운영일지(%m.%d)'),
            date.strftime('송도지소_하수_%Y%m%d'))


def hmi_filename(date):
    return hmi_stems(date)[0] + '.xlsx'
