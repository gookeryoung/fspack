import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

ItemDelegate {
    id: navItem
    property string iconText: ""
    property string label: ""
    property string pageId: ""
    property bool selected: false
    signal clicked()

    Layout.fillWidth: true
    Layout.preferredHeight: 40
    leftPadding: 0

    // 背景
    background: Rectangle {
        color: navItem.selected
              ? (Theme.isDark ? "#2A2B3A" : "#EDF3FF")
              : "transparent"
        Behavior on color { ColorAnimation { duration: 120 } }
    }

    // 左侧 3px 选中指示条
    Rectangle {
        width: 3
        height: parent.height * 0.55
        anchors.verticalCenter: parent.verticalCenter
        color: navItem.selected
              ? (Theme.isDark ? "#7AA2F7" : "#4A90D9")
              : "transparent"
        radius: 2
        Behavior on color { ColorAnimation { duration: 120 } }
    }

    // 图标 + 文字
    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 18
        anchors.rightMargin: 12
        spacing: 12

        Label {
            text: navItem.iconText
            font.pixelSize: 14
            Layout.preferredWidth: 20
            horizontalAlignment: Text.AlignHCenter
        }
        Label {
            text: navItem.label
            font.pixelSize: 13
            color: navItem.selected
                  ? (Theme.isDark ? "#E0E0EF" : "#2A5DB0")
                  : (Theme.isDark ? "#8A8A9A" : "#555555")
            Behavior on color { ColorAnimation { duration: 120 } }
        }
    }

    onClicked: navItem.clicked()
}