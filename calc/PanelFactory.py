# Copyright (C) 2026 Enrico Miot (EndrX)
# Licensed under GNU GPL v3 or later — see the LICENSE file or <https://www.gnu.org/licenses/>
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys

import unohelper
from com.sun.star.lang import XServiceInfo
from com.sun.star.ui import XUIElementFactory

sys.path.insert(0, os.path.dirname(__file__))

IMPL_NAME = "org.skorpionos.sk_office.calc.PanelFactory"


class PanelFactory(unohelper.Base, XUIElementFactory, XServiceInfo):
    def __init__(self, ctx):
        self.ctx = ctx

    def createUIElement(self, resource_url, args):
        props = {p.Name: p.Value for p in args}
        if resource_url.endswith("/SkLibreCalcFormulaPanel"):
            from CalcFormulaPanel import FormulaPanel
            return FormulaPanel(
                self.ctx,
                resource_url,
                props.get("ParentWindow"),
                props.get("Frame"),
            )

        from CalcAiPanel import AiPanel
        return AiPanel(
            self.ctx,
            resource_url,
            props.get("ParentWindow"),
            props.get("Frame"),
        )

    def getImplementationName(self):
        return IMPL_NAME

    def supportsService(self, name):
        return name in self.getSupportedServiceNames()

    def getSupportedServiceNames(self):
        return (IMPL_NAME,)


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(PanelFactory, IMPL_NAME, (IMPL_NAME,))
